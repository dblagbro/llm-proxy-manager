"""v5.0.3 — POST /v1/responses translation shim.

Why this exists: the Coordinator Hub team's v2.0.1 prod-key allow-list
includes ``/v1/responses`` (OpenAI Responses API — used by Codex CLI and
modern OpenCode dispatches). Without a handler, the path 405s for them
the moment they cut over.

Strategy: don't duplicate the 800-LOC chat_completions handler. Instead:

1. Translate the incoming Responses-shape body → ChatCompletions-shape.
2. Wrap the Request in a lightweight proxy that returns the translated
   body when chat_completions calls ``await request.json()``.
3. Call ``chat_completions(...)`` directly — same auth, same compliance
   enforcement (UA check, owner_company filter, disclosure headers),
   same provider dispatch, same audit.
4. Translate the JSON response back to Responses-shape.

Limitations (intentional, documented):

- **Streaming** is NOT translated — Responses SSE has a different event
  schema (`response.created`, `response.output_text.delta`, etc.) than
  ChatCompletions. If a Responses caller sets ``stream=true`` we return
  HTTP 501 with a clear ``X-Compliance-Note`` pointing them to
  ``/v1/chat/completions`` for streaming until v5.1+ implements the
  full SSE translation. The Hub team's migration plan retires
  ``/v1/responses`` entirely when bots flip to OpenCode, so this is
  unlikely to bite.
- ``previous_response_id`` / ``store`` / ``metadata`` / ``include`` —
  these are stateful Responses features (server-side state machine).
  We don't store responses; these fields are silently dropped and the
  request flows as a standalone chat completion. Callers using
  multi-turn Responses state machines should call /v1/chat/completions
  with their own conversation history (or use the proxy's
  ``x-conversation-id`` + caller-memory system).
- ``reasoning.effort`` (Responses-shape) → ``reasoning_effort``
  (ChatCompletions-shape) — same effort tiers.
- ``max_output_tokens`` → ``max_tokens``.
- The ``input`` field can be a string, an array of "items", or
  more complex multimodal blocks. We translate strings and the common
  message-shape items; unknown item types are passed through as-is
  inside a single user message's content field for the downstream model
  to figure out (rare but defensive).

Compliance + audit semantics match /v1/chat/completions exactly because
we route through the same handler.
"""
from __future__ import annotations

import json as _json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request as StarletteRequest

from app.models.database import get_db
from app.utils.disconnect_watchdog import watch_for_disconnect


logger = logging.getLogger(__name__)


router = APIRouter()


def _translate_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """Responses-shape body → ChatCompletions-shape body.

    See module docstring for the field-by-field mapping.
    """
    out: Dict[str, Any] = {}

    if "model" in body:
        out["model"] = body["model"]

    messages = []

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, dict):
                # Message-shape items: {"role": "...", "content": [...]}
                if item.get("type") in (None, "message"):
                    role = item.get("role", "user")
                    content = item.get("content")
                    if isinstance(content, list):
                        # Responses content blocks: [{"type":"input_text","text":...}, ...]
                        # Flatten to a single concatenated string for the most
                        # common case; multimodal blocks pass through as-is.
                        text_parts = []
                        multimodal = []
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if btype in ("input_text", "text", "output_text"):
                                text_parts.append(block.get("text", ""))
                            elif btype in ("input_image", "image"):
                                # ChatCompletions vision-shape
                                multimodal.append({
                                    "type": "image_url",
                                    "image_url": {"url": block.get("image_url") or block.get("url", "")},
                                })
                            else:
                                # Unknown block — pass as text representation
                                text_parts.append(_json.dumps(block))
                        joined_text = "\n".join(t for t in text_parts if t)
                        if multimodal:
                            mm_content = []
                            if joined_text:
                                mm_content.append({"type": "text", "text": joined_text})
                            mm_content.extend(multimodal)
                            messages.append({"role": role, "content": mm_content})
                        else:
                            messages.append({"role": role, "content": joined_text})
                    elif isinstance(content, str):
                        messages.append({"role": role, "content": content})
                elif item.get("type") == "function_call":
                    # Tool-call replay (rare in Responses input but possible)
                    messages.append({
                        "role": "assistant",
                        "tool_calls": [{
                            "id": item.get("call_id", f"call_{uuid.uuid4().hex[:8]}"),
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", "{}"),
                            },
                        }],
                    })
                elif item.get("type") == "function_call_output":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": item.get("call_id", ""),
                        "content": item.get("output", ""),
                    })
                # Unknown item types are dropped (defensive — better than
                # forwarding garbage to litellm).
    out["messages"] = messages

    # Direct field mappings (same semantic)
    for src, dst in [
        ("max_output_tokens", "max_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("seed", "seed"),
        ("response_format", "response_format"),
        ("tools", "tools"),
        ("tool_choice", "tool_choice"),
        ("parallel_tool_calls", "parallel_tool_calls"),
        ("stream", "stream"),
        ("user", "user"),
    ]:
        if src in body and body[src] is not None:
            out[dst] = body[src]

    # reasoning.effort → reasoning_effort
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        out["reasoning_effort"] = reasoning["effort"]

    return out


def _translate_response(cc_response: Dict[str, Any]) -> Dict[str, Any]:
    """ChatCompletions JSON response → Responses-shape JSON.

    See module docstring for the field-by-field mapping.
    """
    choice = (cc_response.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content_str = msg.get("content") or ""
    finish_reason = choice.get("finish_reason")

    status = "completed"
    if finish_reason == "length":
        status = "incomplete"
    elif finish_reason in ("content_filter", "stop_sequence"):
        status = "completed"

    output_items = []
    if content_str:
        output_items.append({
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content_str, "annotations": []}],
        })

    # Tool calls — surface as function_call items
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        output_items.append({
            "type": "function_call",
            "id": f"fc_{uuid.uuid4().hex[:24]}",
            "call_id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", ""),
            "status": "completed",
        })

    usage_cc = cc_response.get("usage") or {}
    usage_r = {
        "input_tokens": usage_cc.get("prompt_tokens", 0),
        "output_tokens": usage_cc.get("completion_tokens", 0),
        "total_tokens": usage_cc.get("total_tokens", 0),
    }
    # Carry reasoning-token detail if present (newer chat-completions)
    if "completion_tokens_details" in usage_cc:
        details = usage_cc["completion_tokens_details"] or {}
        if "reasoning_tokens" in details:
            usage_r["output_tokens_details"] = {"reasoning_tokens": details["reasoning_tokens"]}

    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": cc_response.get("model") or "",
        "output": output_items,
        "usage": usage_r,
        "metadata": None,
    }


class _BodyOverrideRequest:
    """Thin proxy over a Starlette Request that returns a precomputed
    JSON body on ``.json()`` instead of re-reading the raw body. Falls
    through to the wrapped request for every other attribute.

    Used because we already consumed + translated the original body and
    need to hand chat_completions the translated dict.
    """

    def __init__(self, wrapped: StarletteRequest, override_body: Dict[str, Any]):
        self._wrapped = wrapped
        self._override = override_body
        # Cache the serialized form so .body() works too (rare; chat_completions
        # doesn't call it but defensive).
        self._raw = _json.dumps(override_body).encode("utf-8")

    async def json(self):
        return self._override

    async def body(self):
        return self._raw

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


@router.post("/v1/responses")
async def responses(
    request: Request,
    background_tasks: BackgroundTasks,
    # v5.9.9 — same disconnect watchdog wired into /v1/messages in
    # v5.7.17. Must precede ``db`` so the watcher is armed before the
    # session is checked out.
    _watchdog: None = Depends(watch_for_disconnect),
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    llm_hint: Optional[str] = Header(None, alias="llm-hint"),
    x_session_id: Optional[str] = Header(None, alias="x-session-id"),
    x_cot_iterations: Optional[str] = Header(None, alias="x-cot-iterations"),
    x_cot_verify: Optional[str] = Header(None, alias="x-cot-verify"),
    x_cot_samples: Optional[str] = Header(None, alias="x-cot-samples"),
    x_cot_mode: Optional[str] = Header(None, alias="x-cot-mode"),
    x_webhook_url: Optional[str] = Header(None, alias="x-webhook-url"),
    x_cache: Optional[str] = Header(None, alias="x-cache"),
    x_cache_ttl: Optional[str] = Header(None, alias="x-cache-ttl"),
    x_hedge: Optional[str] = Header(None, alias="x-hedge"),
    x_context_strategy: Optional[str] = Header(None, alias="x-context-strategy"),
    x_conversation_id: Optional[str] = Header(None, alias="x-conversation-id"),
    x_memory_tag: Optional[str] = Header(None, alias="x-memory-tag"),
):
    """Translate OpenAI Responses-shape → ChatCompletions, dispatch through
    the existing handler, translate back.

    Streaming intentionally not supported in v5.0.x (see module docstring).
    """
    try:
        raw_body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(raw_body, dict):
        raise HTTPException(400, "Responses body must be a JSON object")

    if raw_body.get("stream"):
        raise HTTPException(
            status_code=501,
            detail={
                "error": {
                    "type": "not_implemented",
                    "message": (
                        "/v1/responses streaming is not yet supported by this proxy. "
                        "Use /v1/chat/completions with stream=true, or call /v1/responses "
                        "with stream=false. Streaming Responses SSE translation is on the "
                        "v5.1 roadmap."
                    ),
                }
            },
            headers={"X-Compliance-Note": "responses-streaming-not-implemented"},
        )

    cc_body = _translate_request(raw_body)
    proxied_request = _BodyOverrideRequest(request, cc_body)

    # Lazy import to avoid a circular dependency at module load.
    from app.api.completions import chat_completions

    cc_response = await chat_completions(
        request=proxied_request,
        background_tasks=background_tasks,
        db=db,
        authorization=authorization,
        x_api_key=x_api_key,
        llm_hint=llm_hint,
        x_session_id=x_session_id,
        x_cot_iterations=x_cot_iterations,
        x_cot_verify=x_cot_verify,
        x_cot_samples=x_cot_samples,
        x_cot_mode=x_cot_mode,
        x_webhook_url=x_webhook_url,
        x_cache=x_cache,
        x_cache_ttl=x_cache_ttl,
        x_hedge=x_hedge,
        x_context_strategy=x_context_strategy,
        x_conversation_id=x_conversation_id,
        x_memory_tag=x_memory_tag,
    )

    # Preserve compliance + budget headers from the chat_completions response
    if isinstance(cc_response, JSONResponse):
        cc_json = _json.loads(cc_response.body)
        translated = _translate_response(cc_json)
        out_headers = dict(cc_response.headers)
        # v5.14.1 — also fire hooks under handler_id="responses". The
        # inner completions.py call already fired with id "completions";
        # rerunning here lets hub-side hooks that key on the caller-
        # facing handler see the right id. Built-in substitution hook
        # is idempotent so it's a no-op when the inner pass already set
        # the header.
        try:
            from app.api._response_hook_runner import apply_response_hooks, HookContext
            await apply_response_hooks(
                handler_id="responses",
                resp_headers=out_headers,
                context=HookContext(
                    api_key_id=getattr(key_record, "id", None) if 'key_record' in locals() else None,
                    request=request,
                ),
            )
        except Exception:
            pass
        return JSONResponse(content=translated, headers=out_headers)
    if isinstance(cc_response, dict):
        return JSONResponse(content=_translate_response(cc_response))
    # Defensive: if chat_completions returned something unexpected, surface
    # as-is rather than mangling.
    return cc_response
