"""v5.21.3 — Buffered-cascade streaming with SSE heartbeats.

Companion to the v5.21.0 buffered-cascade mode. That path forces
``stream=False`` at the top of the handler, runs the FULL non-streaming
flow (tool hops, memory extraction, MCP injection, cascade), and at
the end converts the final ``anthropic_result`` to SSE frames.
Trade-off: no client-visible bytes during the buffered wait, which
can be seconds. Client's connection looks stuck.

v5.21.3 opts into a heartbeat mode: the buffered path is instead
routed through THIS module, which:

1. Returns a ``StreamingResponse`` at the top of the handler (before
   the LLM dispatch runs), so bytes start flowing at the client
   immediately.
2. Yields a ``: cascade-buffering`` comment SSE frame right away.
3. Awaits the actual LLM dispatch (+ optional cascade) as an asyncio
   task, emitting ``: keepalive`` frames every N seconds while
   awaiting.
4. Converts the final ``anthropic_result`` to Anthropic-shape SSE
   frames and yields those.

Trade-off (documented, honest): this path runs a MINIMAL dispatch —
the LLM call + optional refusal cascade. It DOES NOT run proxy tool
hops, memory injection, MCP tool injection, or the response tail.
Those features are unavailable in heartbeat mode. Callers who need
them keep using the v5.21.0 no-heartbeat mode by NOT setting the
opt-in flag.

Opt-in: per-key column ``refusal_retry_streaming_heartbeat`` (bool).
Default False = v5.21.0 no-heartbeat behavior. True = v5.21.3
heartbeat behavior.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Optional

from app.cot.sse import (
    anthropic_text_sse, anthropic_tool_sse, anthropic_tools_sse,
    to_anthropic_response,
)
from app.routing.retry import acompletion_with_retry

logger = logging.getLogger(__name__)

# Heartbeat interval — every N seconds during buffered dispatch we
# yield a ``: keepalive`` SSE comment frame. Chosen conservatively:
# too frequent = wasted bytes; too infrequent = client's read-timeout
# may fire before we send anything. 5s is a compromise; most
# consumers' SSE read timeouts are 30s+.
DEFAULT_HEARTBEAT_INTERVAL_SEC = 5.0

# Initial comment frame emitted as soon as the generator starts.
# Kicks bytes to the client immediately so the connection doesn't
# read as idle during the first ``DEFAULT_HEARTBEAT_INTERVAL_SEC``.
_INITIAL_FRAME = b": cascade-buffering\n\n"

# Periodic heartbeat frame. Format is an SSE comment (line starting
# with ``:``), which every SSE client parser silently discards.
_HEARTBEAT_FRAME = b": keepalive\n\n"


async def _dispatch_with_cascade(
    *,
    route: Any,
    key_record: Any,
    messages_list: list,
    extra: dict,
    system: Optional[Any],
    max_tokens: int,
    has_images: bool,
    hint: Any,
    db: Any,
) -> dict:
    """Run the LLM dispatch + optional refusal cascade, return the final
    ``anthropic_result``.

    This is the minimal dispatch for heartbeat mode — no tool hops,
    no memory extraction, no MCP injection, no response tail. Just
    the LLM call and cascade retry.
    """
    _extra = dict(extra)
    if system is not None:
        _extra["system"] = system

    result = await acompletion_with_retry(
        model=route.litellm_model,
        messages=messages_list,
        stream=False,
        **_extra,
    )
    anthropic_result = to_anthropic_response(result)

    # v5.20.1 refusal cascade — reuses the same module used by the
    # full non-streaming path.
    try:
        from app.api._refusal_cascade import maybe_cascade_on_refusal

        async def _cascade_dispatch(alt_route):
            _e = dict(extra)
            if system is not None:
                _e["system"] = system
            return await acompletion_with_retry(
                model=alt_route.litellm_model,
                messages=messages_list,
                stream=False,
                **_e,
            )

        cascade = await maybe_cascade_on_refusal(
            db=db,
            key_record=key_record,
            initial_route=route,
            initial_result=result,
            initial_anthropic=anthropic_result,
            hint=hint,
            has_images=has_images,
            messages_list=messages_list,
            max_tokens=max_tokens,
            system=system,
            extra=extra,
            dispatch=_cascade_dispatch,
        )
        if cascade and cascade.get("swapped"):
            anthropic_result = cascade["anthropic_result"]
    except Exception as exc:
        # Cascade failure is non-fatal — the original response still
        # goes out. Log for diagnosis; the client sees the initial
        # response as if cascade were disabled.
        logger.warning("v5.21.3.cascade_failed err=%r", exc)

    return anthropic_result


async def _result_to_sse(anthropic_result: dict) -> AsyncIterator[bytes]:
    """Convert the final ``anthropic_result`` dict to Anthropic-shape
    SSE frames. Reuses the same helpers the v5.21.0 buffered path uses,
    so client-visible framing is identical."""
    content_blocks = anthropic_result.get("content") or []
    tool_uses = [b for b in content_blocks if b.get("type") == "tool_use"]
    if len(tool_uses) >= 2:
        async for chunk in anthropic_tools_sse([
            {"name": t["name"], "input": t.get("input", {})}
            for t in tool_uses
        ]):
            yield chunk
    elif len(tool_uses) == 1:
        async for chunk in anthropic_tool_sse(
            tool_uses[0]["name"], tool_uses[0].get("input", {}),
        ):
            yield chunk
    else:
        text = "".join(
            b.get("text", "") for b in content_blocks
            if b.get("type") == "text"
        )
        async for chunk in anthropic_text_sse(text):
            yield chunk


async def run_buffered_cascade_stream_with_heartbeat(
    *,
    route: Any,
    key_record: Any,
    messages_list: list,
    extra: dict,
    system: Optional[Any],
    max_tokens: int,
    has_images: bool,
    hint: Any,
    db: Any,
    interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SEC,
) -> AsyncIterator[bytes]:
    """Streaming generator: initial marker → heartbeats during dispatch
    → real SSE frames from the final result.

    Yields bytes that go directly to the client via
    ``StreamingResponse``. The dispatch task runs concurrently with
    the heartbeat loop; when it completes, its result is converted to
    real SSE frames.

    Errors during dispatch surface as SSE error frames — never let a
    stack trace leak into the stream.
    """
    yield _INITIAL_FRAME

    task = asyncio.create_task(_dispatch_with_cascade(
        route=route,
        key_record=key_record,
        messages_list=messages_list,
        extra=extra,
        system=system,
        max_tokens=max_tokens,
        has_images=has_images,
        hint=hint,
        db=db,
    ))
    while not task.done():
        try:
            # ``asyncio.shield`` prevents the wait_for from cancelling
            # the task on timeout; only the WAIT gets cancelled, the
            # task keeps running toward the next heartbeat check.
            await asyncio.wait_for(asyncio.shield(task), timeout=interval_seconds)
        except asyncio.TimeoutError:
            yield _HEARTBEAT_FRAME
    try:
        anthropic_result = task.result()
    except Exception as exc:
        # Surface as an SSE error frame (Anthropic shape).
        import json as _json
        err_payload = _json.dumps({
            "type": "error",
            "error": {"type": "internal_error", "message": str(exc)},
        })
        yield f"data: {err_payload}\n\n".encode()
        yield b"data: [DONE]\n\n"
        return

    async for chunk in _result_to_sse(anthropic_result):
        yield chunk
