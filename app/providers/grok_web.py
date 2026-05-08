"""
Grok web-simulation provider (v3.2.0).

Lets operators bring their grok.com web subscription (Lite / Premium) into
the proxy without a paid xAI API key. We replay the exact request shape
the grok.com web UI sends to ``/rest/app-chat/conversations/{id}/responses``.

Reverse-engineered 2026-05-08 from a logged-in browser session:

- Endpoint: ``POST https://grok.com/rest/app-chat/conversations/{conv_id}/responses``
- Auth: cookies (``cf_clearance``, ``__cf_bm``, ``sso``, ``sso-rw``,
  ``x-userid``) + headers (``user-agent``, ``x-statsig-id``,
  ``x-xai-request-id``, ``referer``, ``sec-ch-ua-*``, ``origin``).
- Body: ``{message, parentResponseId, modeId, …feature_flags}``. We pass
  ``parentResponseId: ""`` so each proxy call is a fresh thread inside the
  operator's conversation (avoids context bleed between callers).
- Model selection: ``modeId: "fast"`` → grok-3, ``modeId: "expert"`` → grok-4.
- Response: NDJSON stream — collect ``result.token`` where
  ``result.messageTag == "final"``; final boundary signaled by
  ``result.isSoftStop == true`` or ``result.modelResponse``.

Anti-bot constraints:

- ``POST /conversations/new`` is rejected (HTTP 403) by Cloudflare's
  anti-bot rules from server IPs. So we cannot create fresh conversations
  programmatically — operator pastes ONE existing conversation_id and we
  reuse it indefinitely. ``parentResponseId: ""`` keeps proxy turns
  independent within that single conversation.
- ``POST /conversations/{existing}/responses`` is allowed (Cloudflare
  tolerates IP changes for already-blessed conversations).
- ``cf_clearance`` rotates every few hours; ``__cf_bm`` every 30 min;
  ``sso`` lasts longer (JWT). When any expires upstream returns 403; the
  dispatcher surfaces that to the operator UI for re-paste.

Provider config (stored in ``Provider.extra_config``):

    cookie_header     — raw "cookie:" header value from a captured cURL
                        (single string; we don't parse cookies)
    user_agent        — UA string from cURL
    x_statsig_id      — value of x-statsig-id header
    x_userid          — value of x-userid header (also appears as cookie)
    conversation_id   — UUID of an existing conversation in the operator's
                        account (paste from the URL after creating one in
                        the browser: grok.com/c/<this-uuid>)
"""
from __future__ import annotations

import json
import time
import uuid
from typing import AsyncIterator, Optional

import httpx


GROK_BASE_URL = "https://grok.com"

# Model → modeId mapping. Grok exposes web modes (fast, expert, super) that
# correspond loosely to model tiers. We map our normalized model names to
# the closest mode the subscription tier allows. Lite plan grants fast +
# expert; Premium adds super.
MODEL_TO_MODE_ID = {
    "grok-3": "fast",
    "grok-3-fast": "fast",
    "x-ai/grok-3": "fast",
    "grok-4": "expert",
    "x-ai/grok-4": "expert",
    "grok-4-expert": "expert",
}

DEFAULT_MODEL = "grok-3"
SUPPORTED_MODELS = ["grok-3", "grok-4"]


class GrokWebError(Exception):
    """Base error for grok-web dispatch."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class GrokWebAuthError(GrokWebError):
    """Cookies/headers expired or invalid — operator must refresh."""

    def __init__(self, message: str):
        super().__init__(message, status_code=401)


def _build_headers(extra_config: dict, conversation_id: str) -> dict:
    """Build the header set for a grok.com inference request.

    Mirrors what a logged-in browser sends. ``x-xai-request-id`` is unique
    per call (UUID4); the rest comes from ``extra_config``.
    """
    cookie_header = extra_config.get("cookie_header") or ""
    user_agent = extra_config.get("user_agent") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    )
    x_statsig_id = extra_config.get("x_statsig_id") or ""
    x_userid = extra_config.get("x_userid") or ""

    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": GROK_BASE_URL,
        "referer": f"{GROK_BASE_URL}/c/{conversation_id}",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": user_agent,
        "x-xai-request-id": str(uuid.uuid4()),
    }
    if x_statsig_id:
        headers["x-statsig-id"] = x_statsig_id
    if x_userid:
        headers["x-userid"] = x_userid
    if cookie_header:
        headers["cookie"] = cookie_header
    return headers


def _build_body(message: str, mode_id: str, parent_response_id: str = "") -> dict:
    """Build the request body. Matches the captured browser shape exactly."""
    return {
        "message": message,
        "parentResponseId": parent_response_id,
        "disableSearch": False,
        "enableImageGeneration": False,
        "imageAttachments": [],
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "fileAttachments": [],
        "enableImageStreaming": False,
        "imageGenerationCount": 0,
        "forceConcise": True,
        "enableSideBySide": False,
        "sendFinalMetadata": True,
        "metadata": {"request_metadata": {}},
        "disableTextFollowUps": True,
        "isFromGrokFiles": False,
        "disableMemory": False,
        "forceSideBySide": False,
        "isAsyncChat": False,
        "skipCancelCurrentInflightRequests": False,
        "isRegenRequest": False,
        "disableSelfHarmShortCircuit": False,
        "collectionIds": [],
        "disabledConnectorIds": [],
        "deviceEnvInfo": {
            "darkModeEnabled": False,
            "devicePixelRatio": 1.0,
            "screenWidth": 1920,
            "screenHeight": 1080,
            "viewportWidth": 999,
            "viewportHeight": 828,
        },
        "modeId": mode_id,
    }


def _model_to_mode_id(model: str) -> str:
    """Normalize requested model to a Grok web modeId. Default fast (grok-3)."""
    if not model:
        return "fast"
    return MODEL_TO_MODE_ID.get(model, MODEL_TO_MODE_ID.get(model.lower(), "fast"))


def _flatten_messages_to_prompt(messages: list[dict]) -> str:
    """Anthropic/OpenAI messages array → single prompt string for grok.com.

    Grok's web endpoint takes a single ``message`` string, not a turn list.
    We collapse a multi-turn conversation by labeling each turn and joining.
    System messages prepend; the final user turn's content is the operative
    prompt; assistant turns become context.

    Tool calls are not currently supported in the web surface — they get
    serialized as readable JSON in the prompt so the model has visibility
    even though it can't issue tool_use blocks back.
    """
    parts: list[str] = []
    last_user: Optional[str] = None
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        # Anthropic-style: content can be a list of blocks
        if isinstance(content, list):
            text_chunks: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_chunks.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        text_chunks.append(
                            f"[tool_use: {block.get('name','')} "
                            f"input={json.dumps(block.get('input', {}))}]"
                        )
                    elif block.get("type") == "tool_result":
                        text_chunks.append(
                            f"[tool_result: {json.dumps(block.get('content', ''))}]"
                        )
                else:
                    text_chunks.append(str(block))
            text = "\n".join(c for c in text_chunks if c)
        else:
            text = str(content)

        if role == "system":
            parts.append(f"[System]\n{text}")
        elif role == "user":
            last_user = text
            parts.append(f"[User]\n{text}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{text}")
        else:
            parts.append(text)

    # If only one user turn, send that directly without scaffolding (cleaner).
    if len(parts) == 1 and last_user:
        return last_user
    return "\n\n".join(parts)


def _validate_extra_config(extra_config: dict) -> None:
    """Raise GrokWebError if config is missing required fields."""
    required = ["cookie_header", "conversation_id"]
    missing = [k for k in required if not (extra_config or {}).get(k)]
    if missing:
        raise GrokWebError(
            f"grok-web provider missing required extra_config fields: {missing}. "
            "Paste cookie_header (from cURL) and conversation_id (UUID from "
            "grok.com/c/<this-id>) in the provider edit form.",
            status_code=400,
        )


async def complete_grok_web(
    provider_extra_config: dict,
    messages: list[dict],
    model: str,
    timeout: float = 60.0,
) -> dict:
    """Non-streaming completion. Returns OpenAI-shape response dict.

    Collects the full NDJSON stream upstream, then synthesizes a single
    chat.completion response. Token counts come from upstream's final
    metadata when present; otherwise estimated from text length.
    """
    _validate_extra_config(provider_extra_config)
    conv_id = provider_extra_config["conversation_id"]
    mode_id = _model_to_mode_id(model)
    prompt = _flatten_messages_to_prompt(messages)

    url = f"{GROK_BASE_URL}/rest/app-chat/conversations/{conv_id}/responses"
    headers = _build_headers(provider_extra_config, conv_id)
    body = _build_body(prompt, mode_id)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise GrokWebError(f"grok-web upstream network error: {e}")

    if resp.status_code == 401 or resp.status_code == 403:
        raise GrokWebAuthError(
            f"grok-web upstream {resp.status_code}: cookies/headers may be "
            f"expired. Re-capture cookie_header from a fresh browser session "
            f"and update the provider. Body: {resp.text[:200]}"
        )
    if resp.status_code != 200:
        raise GrokWebError(
            f"grok-web upstream {resp.status_code}: {resp.text[:200]}",
            status_code=502,
        )

    full_text = ""
    upstream_model = "grok-3" if mode_id == "fast" else "grok-4"
    response_id: Optional[str] = None

    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = obj.get("result") or {}
        if result.get("token") and result.get("messageTag") == "final":
            full_text += result["token"]
        mr = result.get("modelResponse")
        if mr:
            response_id = mr.get("responseId")
            if mr.get("model"):
                upstream_model = mr["model"]

    # Estimate token counts (no native breakdown from grok.com web API).
    # Heuristic: ~4 chars/token for English. Good enough for usage tracking
    # until xAI exposes a token-count surface.
    prompt_tokens = max(1, len(prompt) // 4)
    completion_tokens = max(1, len(full_text) // 4)
    now = int(time.time())

    return {
        "id": response_id or f"chatcmpl-grokweb-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": now,
        "model": upstream_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": full_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def stream_grok_web(
    provider_extra_config: dict,
    messages: list[dict],
    model: str,
    timeout: float = 60.0,
) -> AsyncIterator[bytes]:
    """Streaming completion. Yields OpenAI SSE-format chunks (bytes).

    Each ``result.token`` in the upstream NDJSON becomes a ``data: {...}``
    SSE line with a ``choices[0].delta.content`` payload. Closes with the
    standard ``data: [DONE]`` sentinel.
    """
    _validate_extra_config(provider_extra_config)
    conv_id = provider_extra_config["conversation_id"]
    mode_id = _model_to_mode_id(model)
    prompt = _flatten_messages_to_prompt(messages)

    url = f"{GROK_BASE_URL}/rest/app-chat/conversations/{conv_id}/responses"
    headers = _build_headers(provider_extra_config, conv_id)
    body = _build_body(prompt, mode_id)

    chunk_id = f"chatcmpl-grokweb-{uuid.uuid4().hex[:16]}"
    upstream_model = "grok-3" if mode_id == "fast" else "grok-4"
    created = int(time.time())

    # Initial role chunk (OpenAI streaming convention)
    first_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": upstream_model,
        "choices": [
            {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
        ],
    }
    yield f"data: {json.dumps(first_chunk)}\n\n".encode()

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code in (401, 403):
                    body_text = (await resp.aread()).decode("utf-8", errors="replace")
                    raise GrokWebAuthError(
                        f"grok-web upstream {resp.status_code}: cookies/headers "
                        f"may be expired. Body: {body_text[:200]}"
                    )
                if resp.status_code != 200:
                    body_text = (await resp.aread()).decode("utf-8", errors="replace")
                    raise GrokWebError(
                        f"grok-web upstream {resp.status_code}: {body_text[:200]}",
                        status_code=502,
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    result = obj.get("result") or {}
                    token = result.get("token")
                    if token and result.get("messageTag") == "final":
                        delta_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": upstream_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": token},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(delta_chunk)}\n\n".encode()
                    mr = result.get("modelResponse")
                    if mr and mr.get("model"):
                        upstream_model = mr["model"]
        except httpx.HTTPError as e:
            raise GrokWebError(f"grok-web upstream network error: {e}")

    # Final chunk + DONE sentinel
    final_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": upstream_model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final_chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def anthropic_response_from_openai(openai_resp: dict) -> dict:
    """Translate the openai-shape result from complete_grok_web back to
    Anthropic /v1/messages shape (used by messages.py callers).

    Lossy on tool_use (grok-web web surface doesn't emit them); content
    becomes a single text block.
    """
    text = ""
    if openai_resp.get("choices"):
        msg = openai_resp["choices"][0].get("message", {})
        text = msg.get("content", "") or ""
    usage = openai_resp.get("usage") or {}
    return {
        "id": openai_resp.get("id", f"msg_grokweb_{uuid.uuid4().hex[:16]}"),
        "type": "message",
        "role": "assistant",
        "model": openai_resp.get("model", "grok-3"),
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def stream_grok_web_anthropic(
    provider_extra_config: dict,
    messages: list[dict],
    system: Optional[str],
    model: str,
    timeout: float = 60.0,
) -> AsyncIterator[bytes]:
    """Streaming /v1/messages — Anthropic SSE event format.

    Translates Grok's NDJSON tokens into the Anthropic event sequence:
    message_start → content_block_start → content_block_delta (per token)
    → content_block_stop → message_delta (stop_reason) → message_stop.
    """
    _validate_extra_config(provider_extra_config)
    conv_id = provider_extra_config["conversation_id"]
    mode_id = _model_to_mode_id(model)

    msgs_with_system = list(messages)
    if system:
        msgs_with_system = [{"role": "system", "content": system}] + msgs_with_system
    prompt = _flatten_messages_to_prompt(msgs_with_system)

    url = f"{GROK_BASE_URL}/rest/app-chat/conversations/{conv_id}/responses"
    headers = _build_headers(provider_extra_config, conv_id)
    body = _build_body(prompt, mode_id)

    msg_id = f"msg_grokweb_{uuid.uuid4().hex[:16]}"
    upstream_model = "grok-3" if mode_id == "fast" else "grok-4"

    def _evt(name: str, data: dict) -> bytes:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()

    yield _evt(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": upstream_model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    yield _evt(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )

    output_chars = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code in (401, 403):
                    body_text = (await resp.aread()).decode("utf-8", errors="replace")
                    raise GrokWebAuthError(
                        f"grok-web upstream {resp.status_code}: {body_text[:200]}"
                    )
                if resp.status_code != 200:
                    body_text = (await resp.aread()).decode("utf-8", errors="replace")
                    raise GrokWebError(
                        f"grok-web upstream {resp.status_code}: {body_text[:200]}",
                        status_code=502,
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    result = obj.get("result") or {}
                    token = result.get("token")
                    if token and result.get("messageTag") == "final":
                        output_chars += len(token)
                        yield _evt(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "text_delta", "text": token},
                            },
                        )
                    mr = result.get("modelResponse")
                    if mr and mr.get("model"):
                        upstream_model = mr["model"]
        except httpx.HTTPError as e:
            raise GrokWebError(f"grok-web upstream network error: {e}")

    yield _evt("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _evt(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {
                "input_tokens": max(1, len(prompt) // 4),
                "output_tokens": max(1, output_chars // 4),
            },
        },
    )
    yield _evt("message_stop", {"type": "message_stop"})
