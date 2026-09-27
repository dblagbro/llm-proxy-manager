"""Generic SSE plumbing + the empty-completion guard.

v5.22.36 — split out of ``app/api/_messages_streaming.py``, which had reached
741 LOC against the 700-LOC ceiling
``test_v4412_streaming_split.test_neither_streaming_file_exceeds_700_loc``
enforces. The seam: this module holds machinery that is agnostic about the
wire format being streamed, while the parent keeps the Anthropic stream
generators (``_stream_anthropic``, ``_stream_cot_anthropic``,
``_webhook_completion_anthropic``) that the same test pins in place.

The cut is clean in both directions — none of the six functions here
references anything left behind, and none of the generators references
anything here, so there is no circular import and no duplicated helper (the
v4.4.12 split had to copy ``_exc_str``; this one does not).

Contents:

- ``_sse_frame_error`` — render an exception as a terminal SSE error frame.
- ``preflight_sse`` — probe the first frame before committing to a 200.
- ``http_status_for_stream_error`` — map a stream failure to a status code.
- ``_sse_frame_has_content`` — does this frame carry caller-visible content?
- ``buffer_sse_until_content`` — hold frames until content appears.
- ``stream_with_empty_guard`` — v5.3.9: an upstream that returns 200 with no
  content must not be passed through as a successful empty stream.

Callers import these from ``app.api._messages_streaming`` as before; that
module re-exports them for back-compat.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _sse_frame_error(frame: bytes):
    """If an SSE ``data:`` frame is a terminal error event, return its
    message string; else None. Handles both the Anthropic shape
    (``{"type":"error","error":{"message":...}}``) and the OpenAI shape
    (``{"error": ...}``) that the litellm streaming wrappers emit."""
    if b"data:" not in frame:
        return None
    try:
        payload = json.loads(frame.split(b"data:", 1)[1].strip())
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    # A successful first frame is message_start (Anthropic) or a
    # chat.completion.chunk (OpenAI) — neither carries an ``error`` key.
    if payload.get("type") == "error" or ("error" in payload and "choices" not in payload):
        err = payload.get("error")
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err)
        if isinstance(err, str):
            return err
        return "upstream error"
    return None


async def preflight_sse(gen):
    """v3.10.13 BUG-001 — pull the first SSE frame from a streaming
    generator so a *pre-stream* upstream failure (auth, rate-limit,
    upstream 5xx) can surface as a real HTTP status instead of an
    HTTP 200 carrying a terminal ``{"type":"error"}`` frame (which
    clients that check ``status_code`` read as success).

    A successful litellm stream's first frame is always ``message_start``
    / a chat-completion chunk; a pre-stream failure's first frame is the
    terminal error event. This gives the litellm streaming paths the
    same fail-loud contract the claude-oauth path already has via its
    ``__anext__()`` pre-flight.

    Returns ``(first_frame, err_message_or_None, gen)``. When err_message
    is not None the upstream never produced content — the caller should
    raise an HTTPException. Otherwise replay ``first_frame`` then
    ``async for`` the rest of ``gen``.
    """
    try:
        first = await gen.__anext__()
    except StopAsyncIteration:
        return b"", "upstream produced an empty stream", gen
    return first, _sse_frame_error(first), gen


def http_status_for_stream_error(msg: str) -> int:
    """Best-effort HTTP status for a pre-stream upstream error string."""
    low = (msg or "").lower()
    if any(s in low for s in (
        "x-api-key", "api key", "api-key", "unauthor", "invalid_grant",
        "authenticationerror", "permissiondenied", " 401", " 403",
    )):
        return 401
    if "rate limit" in low or "rate_limit" in low or "429" in low:
        return 429
    return 502


def _sse_frame_has_content(frame: bytes) -> bool:
    """v5.3.9 — True when an SSE frame carries a *meaningful* delta: text
    content, a tool call, reasoning/thinking output, or partial tool-input
    JSON. Recognizes both wire shapes this proxy emits:

      OpenAI chunk:    ``choices[0].delta.{content,tool_calls,function_call,
                       reasoning_content}`` non-empty
      Anthropic event: ``content_block_delta`` with a non-empty
                       ``text``/``partial_json``/``thinking`` delta, or a
                       ``content_block_start`` opening a ``tool_use`` block

    ``message_start``, role-only chunks, finish-reason-only chunks,
    ``[DONE]``, compliance preludes, and budget frames are all non-content.
    """
    if b"data:" not in frame:
        return False
    try:
        payload = json.loads(frame.split(b"data:", 1)[1].strip())
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    # Anthropic event shapes
    ptype = payload.get("type")
    if ptype == "content_block_delta":
        delta = payload.get("delta") or {}
        return bool(
            delta.get("text") or delta.get("partial_json") or delta.get("thinking")
        )
    if ptype == "content_block_start":
        block = payload.get("content_block") or {}
        return block.get("type") == "tool_use"
    # OpenAI chunk shape
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        delta = (choices[0] or {}).get("delta") or {}
        return bool(
            delta.get("content")
            or delta.get("tool_calls")
            or delta.get("function_call")
            or delta.get("reasoning_content")
        )
    return False


async def buffer_sse_until_content(first, gen, max_frames: int = 64):
    """v5.3.9 — streaming counterpart of the non-streaming empty-success
    guard (``looks_like_empty_success_failure``).

    Root cause this exists for: a dead upstream (observed: cursor-bridge
    with an expired Cursor session) answers HTTP 200 + a syntactically
    valid SSE stream that contains ZERO content deltas — message_start /
    role chunk / finish chunk / [DONE] and nothing else. ``preflight_sse``
    passes it (the first frame is not an error frame), the 200 streams
    end-to-end, and the caller sees an empty completion with rc=0. On
    GCP -s23 both cursor-oauth providers served 0 output tokens across
    400+ requests and every streaming caller got silent EMPTYs while the
    non-streaming path correctly 502'd and failed over.

    Pulls frames (starting with ``first``, already consumed by
    ``preflight_sse``) until a meaningful content delta appears or the
    stream exhausts. Returns ``(frames, has_content, gen)``:

      has_content=True  — replay ``frames`` then iterate ``gen``.
      has_content=False — the stream exhausted with zero content; treat
                          the provider as failed (empty-success).

    ``max_frames`` bounds buffering memory; a stream still alive past the
    cap is passed through as success (never misclassify a slow-but-alive
    stream — the cap is far above any real preamble length).
    """
    frames = [first]
    if _sse_frame_has_content(first):
        return frames, True, gen
    while len(frames) < max_frames:
        try:
            frame = await gen.__anext__()
        except StopAsyncIteration:
            return frames, False, gen
        frames.append(frame)
        if _sse_frame_has_content(frame):
            return frames, True, gen
    return frames, True, gen


async def stream_with_empty_guard(
    *,
    start_stream,            # callable(route) -> fresh async SSE generator
    route,                   # primary RouteResult (already selected)
    db: AsyncSession,
    hint,
    has_tools: bool,
    has_images: bool,
    key_type: str,
    api_key_id: Optional[str],
    model_override: Optional[str],   # None for auto / logical aliases
    max_attempts: int = 3,
):
    """v5.3.9 — dispatch a streaming request with pre-flight + empty-success
    detection + provider failover. Shared by /v1/chat/completions and
    /v1/messages (the ``start_stream`` closure owns the wire format and
    per-route ``extra`` rebuild).

    On an empty-success stream: ``record_failure`` on the provider (same
    breaker semantics as the non-streaming guard at completions.py), then
    re-select excluding it — mirroring the grok-web failover pattern. A
    re-selected provider that ALREADY empty-failed this request gets a
    second ``record_failure`` (deterministic dead-provider signal — pushes
    its breaker open) and selection retries, so two dead same-priority
    providers (the -s23 cursor pair) can't ping-pong a request to death.

    Returns ``(frames, gen, served_route)``; raises HTTPException(502) when
    every candidate streams empty.
    """
    from app.routing.circuit_breaker import record_failure
    from app.routing.router import select_provider
    # v5.7.14 — persist empty-success failovers to activity_log so the
    # AI provider supervisor (and burst-trigger logic in subsequent
    # ships) can see them at sweep time. Pre-5.7.14 these only landed
    # in stdout via logger.warning, invisible to the database-driven
    # supervisor and to the operator dashboard's recent-events panel.
    from app.monitoring.activity import log_event

    attempt_route = route
    empty_failed: set = set()
    for attempt in range(1, max_attempts + 1):
        gen = start_stream(attempt_route)
        first, err, gen = await preflight_sse(gen)
        if err is not None:
            await gen.aclose()
            raise HTTPException(
                http_status_for_stream_error(err),
                f"Upstream error before streaming began: {err}",
            )
        frames, has_content, gen = await buffer_sse_until_content(first, gen)
        if has_content:
            return frames, gen, attempt_route
        await gen.aclose()
        await record_failure(attempt_route.provider.id, billing_error=False)
        empty_failed.add(attempt_route.provider.id)
        logger.warning(
            "streaming empty-success failure provider=%s model=%s "
            "(attempt %d/%d) — failing over",
            attempt_route.provider.name, attempt_route.litellm_model,
            attempt, max_attempts,
        )
        try:
            await log_event(
                db,
                event_type="streaming.empty_success_failover",
                severity="warning",
                message=(
                    f"empty-success stream from {attempt_route.provider.name} "
                    f"({attempt_route.litellm_model}); attempt {attempt}/{max_attempts}"
                ),
                provider_id=attempt_route.provider.id,
                api_key_id=api_key_id,
                metadata={
                    "litellm_model": attempt_route.litellm_model,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "empty_failed_count": len(empty_failed),
                },
            )
        except Exception:
            # Audit write must never break the failover loop.
            pass
        if attempt >= max_attempts:
            break
        # v5.7.13 — cumulative exclusion + cross-family fallback.
        # Previously: exclude_provider_id was single-valued, which made
        # the inner loop ping-pong between two same-family empty-failed
        # providers (e.g. AvaFea ↔ CoE during a Gemini hiccup) and
        # never reach cursor-oauth / anthropic. Now: pass the entire
        # ``empty_failed`` set via ``exclude_provider_ids`` so the
        # router lands on the highest-priority NON-failed provider —
        # any family — first try. If that's a different family from
        # the original model, build_litellm_model substitutes the
        # provider's default chat slug (v3.0.36 cross-family path) and
        # the upstream sees a model it actually serves.
        next_route = None
        try:
            cand = await select_provider(
                db, hint,
                has_tools=has_tools, has_images=has_images,
                key_type=key_type,
                model_override=model_override,
                exclude_provider_ids=set(empty_failed),
                excluded_provider_types={"claude-oauth", "grok-web", "ChatGPT-oauth-plan"},
                api_key_id=api_key_id,
            )
        except Exception:
            cand = None
        # v5.22.4 — release the connection this empty-success failover
        # re-select opened, before the retried stream runs (K3: was pinned
        # across the retried stream for the whole request).
        try:
            await db.commit()
        except Exception:
            pass
        if cand is not None and cand.provider.id not in empty_failed:
            next_route = cand
        if next_route is None:
            break
        attempt_route = next_route
    # v5.7.14 — terminal exhaust event. The 502 we raise next is the
    # symptom; this audit row is the signal supervisor's burst-trigger
    # path will key on once it ships.
    try:
        await log_event(
            db,
            event_type="streaming.failover_exhausted",
            severity="error",
            message=(
                f"empty-success failover exhausted after {len(empty_failed)} "
                f"provider(s); returning 502 to caller"
            ),
            provider_id=route.provider.id,
            api_key_id=api_key_id,
            metadata={
                "tried_provider_ids": sorted(empty_failed),
                "initial_provider": route.provider.id,
                "max_attempts": max_attempts,
            },
        )
    except Exception:
        pass
    raise HTTPException(
        502,
        "upstream: empty-success failure (streaming upstream produced no "
        f"content; {len(empty_failed)} provider(s) tried)",
    )
