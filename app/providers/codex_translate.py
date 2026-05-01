"""
Translation between OpenAI Chat Completions and OpenAI Responses APIs
for the codex-oauth provider type (v3.0.15).

The proxy's clients call ``/v1/chat/completions`` (Chat Completions shape).
The Codex backend at ``chatgpt.com/backend-api/codex/responses`` only
speaks the Responses API. This module translates in both directions:

  Chat Completions request  →  Responses API request
  Responses API SSE stream  →  Chat Completions SSE stream  (or non-stream
                               aggregate if the caller asked for one)

Constraints discovered against a live Plus account on 2026-04-30:

  - The Codex backend REQUIRES ``stream: true`` (returns 400 otherwise).
    So even when the caller asks for a non-streaming Chat Completion,
    we MUST upstream as stream and accumulate into a single response on
    our side.
  - The backend uses Anthropic's Responses-API event names
    (response.created / response.output_text.delta / response.completed
    etc.), not raw Chat Completions deltas.
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Optional


# ── Request: Chat Completions → Responses API ───────────────────────────────


def chat_completions_to_responses(body: dict) -> dict:
    """Translate a Chat Completions request body to the Responses API shape.

    Maps:
      messages[role=system]   →  instructions  (concatenated if multiple)
      messages[role=user]     →  input[type=message,role=user]
      messages[role=assistant]→  input[type=message,role=assistant]
      tools                   →  tools         (Responses API tool shape)
      max_tokens              →  max_output_tokens
      temperature             →  temperature   (passed through)
      stream                  →  stream=True   (always true upstream)
      model                   →  model         (caller picks the slug)

    Notes:
      - We do NOT translate ``response_format``/``tool_choice`` for the
        first cut — Plus tier Codex doesn't expose function calling on
        the Responses surface in a stable way. Phase 3 can add tools.
      - The Codex backend rejects ``store: true`` for the chatgpt.com
        host; we hard-set ``store: false`` regardless of caller input.
    """
    messages = body.get("messages") or []
    system_chunks: list[str] = []
    input_items: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str):
                system_chunks.append(content)
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        system_chunks.append(str(c.get("text") or ""))
                    elif isinstance(c, str):
                        system_chunks.append(c)
            continue
        # user / assistant / tool — turn into Responses API "message" items
        text = _extract_text(content)
        if not text:
            continue
        if role == "user":
            input_items.append({
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            })
        elif role == "assistant":
            input_items.append({
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            })
        elif role == "tool":
            # Approximate tool results as user input until full tool support lands.
            input_items.append({
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"[tool_result] {text}"}],
            })

    out: dict[str, Any] = {
        "model": body.get("model"),
        "input": input_items,
        "stream": True,
        "store": False,
    }
    # Codex backend rejects requests without a non-empty `instructions` field
    # ("Instructions are required" 400). Default to a minimal value when no
    # system message was provided by the caller.
    out["instructions"] = (
        "\n\n".join(s for s in system_chunks if s) or "You are a helpful assistant."
    )
    # NOTE: Codex backend rejects ``max_output_tokens`` and ``max_tokens`` —
    # the chatgpt.com/backend-api/codex/responses endpoint enforces a
    # subscription-tier-set ceiling and ignores caller-supplied caps.
    # Pass temperature/top_p only if explicitly set; both are optional.
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    # v3.0.23 (Q4): map OpenAI Chat Completions ``reasoning_effort`` (the
    # field DevinGPT's reasoning slider sends) to the Responses API's
    # ``reasoning.effort`` so the slider isn't silently dropped on the
    # codex-oauth path. Caller may pass it at top level OR inside
    # extra_body — try both. Codex Responses accepts low/medium/high/xhigh
    # depending on the slug; we pass through and let upstream reject if
    # invalid.
    eb = body.get("extra_body") if isinstance(body.get("extra_body"), dict) else {}
    re = body.get("reasoning_effort") or eb.get("reasoning_effort")
    if isinstance(re, str) and re.strip():
        out["reasoning"] = {"effort": re.strip().lower()}
    return out


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for c in content:
            if isinstance(c, str):
                chunks.append(c)
            elif isinstance(c, dict):
                t = c.get("text")
                if isinstance(t, str):
                    chunks.append(t)
        return "".join(chunks)
    return str(content)


# ── Response: Responses API SSE → Chat Completions ──────────────────────────


def _now() -> int:
    return int(time.time())


async def responses_sse_to_chat_completions_sse(
    upstream_lines: AsyncIterator[str], *, model: str, completion_id: Optional[str] = None,
) -> AsyncIterator[bytes]:
    """Translate the Codex Responses-API SSE stream into Chat Completions SSE.

    Input:  raw line iterator from ``httpx.Response.aiter_lines()`` over
            the codex/responses endpoint. Each event has form
                event: <type>
                data: <json>
            with blank separator between events.
    Output: Chat Completions SSE chunks, each terminated by ``\\n\\n``,
            plus the final ``data: [DONE]\\n\\n`` sentinel.

    Event mapping:
      response.output_text.delta  →  choices[0].delta.content (chunk)
      response.completed          →  choices[0].finish_reason='stop' + final
      response.error              →  pass through as Chat-Completions error
      everything else             →  drop (reasoning items, item.added/done)
    """
    cid = completion_id or f"chatcmpl-codex-{int(time.time() * 1000)}"
    created = _now()
    sent_first = False

    async for line in upstream_lines:
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            # Event names announced; data follows on the next line.
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            evt = json.loads(payload)
        except ValueError:
            continue
        kind = evt.get("type")

        if kind == "response.output_text.delta":
            delta = evt.get("delta") or ""
            if not delta:
                continue
            chunk = {
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": (
                        {"role": "assistant", "content": delta}
                        if not sent_first else {"content": delta}
                    ),
                    "finish_reason": None,
                }],
            }
            sent_first = True
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        elif kind == "response.completed":
            finish = {
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            # Optional: surface usage if upstream provided it
            usage = (evt.get("response") or {}).get("usage")
            if isinstance(usage, dict):
                finish["usage"] = {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
            yield f"data: {json.dumps(finish)}\n\n".encode()
            yield b"data: [DONE]\n\n"
            return
        elif kind in ("response.error", "error"):
            err = evt.get("error") or evt
            err_chunk = {
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "error",
                }],
                "error": {
                    "message": err.get("message") or str(err)[:300],
                    "type": err.get("type") or "upstream_error",
                },
            }
            yield f"data: {json.dumps(err_chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"
            return
        # else: drop (reasoning, item.added/done, content_part.added/done, etc.)


async def collect_responses_stream_into_completion(
    upstream_lines: AsyncIterator[str], *, model: str, completion_id: Optional[str] = None,
) -> dict:
    """Aggregate a Codex Responses-API SSE stream into a single Chat
    Completions response object — for callers that asked ``stream: false``.

    Mirrors the same event mapping as the streaming path but accumulates
    text deltas into one ``message.content`` and returns a dict the caller
    can ``JSONResponse(...)`` directly.
    """
    cid = completion_id or f"chatcmpl-codex-{int(time.time() * 1000)}"
    text_parts: list[str] = []
    finish_reason = "stop"
    usage = None
    err: Optional[dict] = None

    async for line in upstream_lines:
        if not line or line.startswith(":") or line.startswith("event:"):
            continue
        if not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:].strip())
        except ValueError:
            continue
        kind = evt.get("type")
        if kind == "response.output_text.delta":
            d = evt.get("delta")
            if isinstance(d, str):
                text_parts.append(d)
        elif kind == "response.completed":
            u = (evt.get("response") or {}).get("usage")
            if isinstance(u, dict):
                usage = {
                    "prompt_tokens": u.get("input_tokens", 0),
                    "completion_tokens": u.get("output_tokens", 0),
                    "total_tokens": u.get("total_tokens", 0),
                }
            break
        elif kind in ("response.error", "error"):
            err = evt.get("error") or {"message": str(evt)[:300], "type": "upstream_error"}
            finish_reason = "error"
            break

    out: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "".join(text_parts)},
            "finish_reason": finish_reason,
        }],
    }
    if usage:
        out["usage"] = usage
    if err:
        out["error"] = err
    return out
