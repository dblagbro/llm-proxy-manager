"""
Tail functions for /v1/messages (Anthropic endpoint).

Extracted from ``app/api/messages.py`` in the 2026-04-23 refactor so the
POST handler can stay focused on routing + response assembly. In v4.4.12
the claude-oauth section was further extracted into
``_messages_streaming_oauth.py`` (pre-split this file was 979 LOC).

Functions defined HERE:
  _exc_str                       — non-empty error string helper
  _sse_frame_error               — detect terminal SSE error frames
  preflight_sse                  — first-frame extract for fail-loud streams
  http_status_for_stream_error   — map error msg → HTTP status
  _sse_frame_has_content         — detect a meaningful content/tool delta frame
  buffer_sse_until_content       — v5.3.9 streaming empty-success detection
  stream_with_empty_guard        — v5.3.9 guarded dispatch + provider failover
  _stream_cot_anthropic          — pass-through around run_cot_pipeline + metrics
  _stream_anthropic              — the main Anthropic streaming translator (litellm path)
  _webhook_completion_anthropic  — fire-and-forget async delivery

Re-exported from `_messages_streaming_oauth.py` (existing
``from app.api._messages_streaming import _stream_claude_oauth, ...``
imports keep working unchanged):
  _inject_claude_code_system     — required system-prompt prefix
  _complete_claude_oauth         — non-streaming claude-oauth dispatch
  _stream_claude_oauth           — streaming claude-oauth dispatch
  + internal helpers + timeout constants
"""
from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator, Optional

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.cot.pipeline import run_cot_pipeline
from app.cot.sse import FINISH_TO_STOP, to_anthropic_response, extract_cache_tokens
from app.routing.retry import acompletion_with_retry
from app.monitoring.helpers import record_outcome
from app.cache.middleware import maybe_store
from app.api.webhook import post_webhook
from app.providers.claude_oauth import build_headers as _claude_oauth_headers, PLATFORM_BASE_URL

logger = logging.getLogger(__name__)


def _exc_str(e: BaseException) -> str:
    """v2.8.10: produce a non-empty error string. ``str(httpx.ReadTimeout())``
    returns ``""`` when no message is attached, which made activity_log show
    ``error: null`` for every upstream timeout — losing the most important
    diagnostic signal. Fall back to the exception class name when str(e)
    is blank."""
    s = str(e) if e else ""
    return s if s else f"{type(e).__name__} (no message)"


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
        if attempt >= max_attempts:
            break
        # Re-select, skipping candidates that already empty-failed this
        # request (record another failure on them — see docstring).
        next_route = None
        last_excluded = attempt_route.provider.id
        for _ in range(max_attempts):
            try:
                cand = await select_provider(
                    db, hint,
                    has_tools=has_tools, has_images=has_images,
                    key_type=key_type,
                    model_override=model_override,
                    exclude_provider_id=last_excluded,
                    excluded_provider_types={"claude-oauth", "grok-web", "ChatGPT-oauth-plan"},
                    api_key_id=api_key_id,
                )
            except Exception:
                cand = None
            if cand is None:
                break
            if cand.provider.id not in empty_failed:
                next_route = cand
                break
            await record_failure(cand.provider.id, billing_error=False)
            last_excluded = cand.provider.id
        if next_route is None:
            break
        attempt_route = next_route
    raise HTTPException(
        502,
        "upstream: empty-success failure (streaming upstream produced no "
        f"content; {len(empty_failed)} provider(s) tried)",
    )


async def _stream_cot_anthropic(
    model: str,
    messages: list,
    session_id: str | None,
    extra: dict,
    max_iterations: int | None,
    provider_id: str,
    db: AsyncSession,
    key_record_id: str,
    force_verify: bool | None = None,
    critique_model: str | None = None,
    critique_kwargs: dict | None = None,
    samples: int = 1,
    task_branch: str | None = None,
    requested_model: str = "",  # v3.0.44: caller's bare model id for activity log
    llm_hint: Optional[str] = None,  # v3.0.59: capture in event_meta.lmrh_hint
    # v3.10.11 (#267) — caller-memory write-back for the CoT streaming
    # path (the one streaming path that was missing it). When
    # ``conversation_id`` is set, memory-tool tool_use blocks are
    # accumulated across the SSE passthrough and fed through the same
    # maybe_extract_memory_writes() the other streaming paths use.
    conversation_id: Optional[str] = None,
    memory_tag: Optional[str] = None,
    # v5.0.0 — compliance substitution SSE prelude (decision 15 + 28).
    compliance_disclosure: Optional[dict] = None,
    accept_compliance_events: bool = False,
) -> AsyncIterator[bytes]:
    """Pass-through wrapper around run_cot_pipeline; records metrics after completion."""
    import json as _json
    in_tok = out_tok = 0
    cache_creation = cache_read = 0
    t0 = time.monotonic()
    if compliance_disclosure and accept_compliance_events:
        from app.compliance import sse_prelude_anthropic
        yield sse_prelude_anthropic(compliance_disclosure)
    # v3.10.11 — accumulate memory-tool tool_use blocks across the SSE
    # passthrough, keyed by content-block index, so the assembled
    # response can be fed through maybe_extract_memory_writes once the
    # CoT stream completes.
    tool_acc: dict = {}
    try:
        async for chunk in run_cot_pipeline(
            model, messages, session_id, extra, max_iterations, force_verify,
            critique_model=critique_model, critique_kwargs=critique_kwargs,
            samples=samples, task_branch=task_branch,
        ):
            yield chunk
            for line in chunk.decode(errors="ignore").splitlines():
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                try:
                    evt = _json.loads(line[6:])
                except ValueError:
                    continue
                etype = evt.get("type")
                if etype == "message_delta":
                    usage = evt.get("usage", {})
                    in_tok = usage.get("input_tokens", in_tok)
                    out_tok = usage.get("output_tokens", out_tok)
                    cache_creation = usage.get("cache_creation_input_tokens", cache_creation) or cache_creation
                    cache_read = usage.get("cache_read_input_tokens", cache_read) or cache_read
                elif etype == "content_block_start":
                    cb = evt.get("content_block") or {}
                    if cb.get("type") == "tool_use":
                        tool_acc[evt.get("index")] = {
                            "id": cb.get("id", ""), "name": cb.get("name", ""), "json": "",
                        }
                elif etype == "content_block_delta":
                    delta = evt.get("delta") or {}
                    if delta.get("type") == "input_json_delta" and evt.get("index") in tool_acc:
                        tool_acc[evt["index"]]["json"] += delta.get("partial_json", "")
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id,
                             cache_creation=cache_creation, cache_read=cache_read,
                             requested_model=requested_model or None,
                             had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None)
        # v3.10.11 — caller-memory write-back. Mirror the other streaming
        # paths: call extract whenever the caller opted into memory
        # (conversation_id set), even with no memory-tool blocks, so the
        # extract metric increments consistently. Silent degrade.
        if conversation_id and key_record_id:
            try:
                content_blocks = []
                for slot in tool_acc.values():
                    try:
                        parsed = _json.loads(slot["json"]) if slot["json"] else {}
                    except ValueError:
                        parsed = {}
                    content_blocks.append({
                        "type": "tool_use", "id": slot["id"],
                        "name": slot["name"], "input": parsed,
                    })
                from app.memory.extract import maybe_extract_memory_writes
                await maybe_extract_memory_writes(
                    db,
                    response_dict={"content": content_blocks},
                    api_key_id=key_record_id,
                    conversation_id=conversation_id,
                    memory_tag_default=memory_tag,
                    source_provider_id=provider_id,
                )
            except Exception:
                pass  # never break the stream's success path
    except Exception as e:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=_exc_str(e),
                             requested_model=requested_model or None,
                             had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None)
        yield (b'data: ' + json.dumps({"type": "error", "error": {"message": _exc_str(e)}}).encode() + b'\n\n')
        yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'


async def _stream_anthropic(
    model: str, messages: list, extra: dict, provider_id: str,
    db: AsyncSession, key_record_id: str, t0: float, budget_total: int = 0,
    cache_decision=None,
    llm_hint: Optional[str] = None,  # v3.0.59
    # v3.9.14 (#267 Phase 5.5 follow-up) — memory write-back for the
    # litellm streaming Anthropic path. Same shape as _stream_claude_oauth:
    # when conversation_id is set, accumulate tool_use blocks across the
    # SSE stream + feed the assembled response dict through the same
    # maybe_extract_memory_writes() the non-streaming path uses. No-op
    # when conversation_id is None.
    api_key_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    memory_tag: Optional[str] = None,
    # v5.0.0 — compliance substitution SSE prelude (decision 15 + 28).
    # When both are set, emit ``event: compliance_substitution`` BEFORE
    # ``message_start`` (opt-in via ``Accept-Compliance-Events: true``).
    compliance_disclosure: Optional[dict] = None,
    accept_compliance_events: bool = False,
) -> AsyncIterator[bytes]:
    try:
        if compliance_disclosure and accept_compliance_events:
            from app.compliance import sse_prelude_anthropic
            yield sse_prelude_anthropic(compliance_disclosure)
        response = await acompletion_with_retry(model=model, messages=messages, stream=True, **extra)
        index = 0
        text_started = False
        tool_started = False
        finish_reason = "stop"
        input_tokens = 0
        output_tokens = 0
        cache_creation = 0
        cache_read = 0
        streamed_chars = 0
        tool_id: str = ""
        tool_name: str = ""
        ttft_ms: float = 0.0
        full_text_buf: list[str] = []
        # v3.9.14 — accumulate tool_use blocks for memory extraction at
        # end-of-stream. tool_calls_acc[tool_id] = {id, name, input_str}
        tool_calls_acc: dict[str, dict] = {}

        yield (
            f'data: {{"type":"message_start","message":{{"id":"msg_proxy","type":"message",'
            f'"role":"assistant","content":[],"model":"{model}",'
            f'"stop_reason":null,"stop_sequence":null,'
            f'"usage":{{"input_tokens":0,"output_tokens":0}}}}}}\n\n'
        ).encode()

        async for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            if hasattr(chunk, "usage") and chunk.usage:
                input_tokens = getattr(chunk.usage, "prompt_tokens", input_tokens)
                output_tokens = getattr(chunk.usage, "completion_tokens", output_tokens)
                c_create, c_read = extract_cache_tokens(chunk.usage)
                if c_create:
                    cache_creation = c_create
                if c_read:
                    cache_read = c_read

            # Tool call streaming
            tool_calls = getattr(delta, "tool_calls", None) or []
            for tc_delta in tool_calls:
                fn = getattr(tc_delta, "function", None)
                if not fn:
                    continue
                if not tool_started:
                    if not ttft_ms:
                        ttft_ms = (time.monotonic() - t0) * 1000
                    tool_id = getattr(tc_delta, "id", "") or f"toolu_{id(tc_delta)}"
                    tool_name = getattr(fn, "name", "") or ""
                    yield (
                        f'data: {{"type":"content_block_start","index":{index},'
                        f'"content_block":{{"type":"tool_use","id":"{tool_id}",'
                        f'"name":"{tool_name}","input":{{}}}}}}\n\n'
                    ).encode()
                    tool_started = True
                    # v3.9.14 — seed accumulator for memory extraction
                    tool_calls_acc.setdefault(tool_id, {
                        "id": tool_id, "name": tool_name, "input_str": "",
                    })
                args_fragment = getattr(fn, "arguments", "") or ""
                if args_fragment:
                    escaped = json.dumps(args_fragment)[1:-1]
                    yield (
                        f'data: {{"type":"content_block_delta","index":{index},'
                        f'"delta":{{"type":"input_json_delta","partial_json":"{escaped}"}}}}\n\n'
                    ).encode()
                    # v3.9.14 — accumulate partial JSON for end-of-stream parse
                    if tool_id in tool_calls_acc:
                        tool_calls_acc[tool_id]["input_str"] += args_fragment

            # Text streaming
            content = getattr(delta, "content", None) or ""
            if not text_started and content:
                if not ttft_ms:
                    ttft_ms = (time.monotonic() - t0) * 1000
                yield f'data: {{"type":"content_block_start","index":{index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                text_started = True
            if content:
                streamed_chars += len(content)
                full_text_buf.append(content)
                escaped = json.dumps(content)[1:-1]
                yield f'data: {{"type":"content_block_delta","index":{index},"delta":{{"type":"text_delta","text":"{escaped}"}}}}\n\n'.encode()

        # v4.4.5 BUG-056 — ensure the Anthropic streaming protocol
        # always emits at least one content_block_start/_stop pair.
        # Gemini (and any provider that truncates at max_tokens or
        # returns an empty body) can emit a stream where no chunk
        # ever carries delta.content — leaving the stream with
        # message_start + message_delta + message_stop but no
        # content block events. Anthropic SDK clients depend on
        # content_block_start to construct the assistant message
        # object; without it they emit empty / null content. Emit
        # an explicit empty text block in that case.
        if not text_started and not tool_started:
            yield f'data: {{"type":"content_block_start","index":{index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
            yield f'data: {{"type":"content_block_stop","index":{index}}}\n\n'.encode()
        else:
            yield f'data: {{"type":"content_block_stop","index":{index}}}\n\n'.encode()

        if output_tokens == 0 and streamed_chars > 0:
            output_tokens = max(1, streamed_chars // 4)

        stop_reason = FINISH_TO_STOP.get(finish_reason, "end_turn")
        usage_parts = [f'"output_tokens":{output_tokens}']
        if cache_creation:
            usage_parts.append(f'"cache_creation_input_tokens":{cache_creation}')
        if cache_read:
            usage_parts.append(f'"cache_read_input_tokens":{cache_read}')
        yield (
            f'data: {{"type":"message_delta","delta":{{"stop_reason":"{stop_reason}",'
            f'"stop_sequence":null}},"usage":{{{",".join(usage_parts)}}}}}\n\n'
        ).encode()
        # v5.0.12 — removed mid-stream ``event: budget`` SSE frame.
        # See _completions_streaming.py for context (Vercel-AI-SDK consumer
        # incompatibility). Budget remains on X-Token-Budget-Remaining header.
        yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=input_tokens, out_tok=output_tokens, t0=t0,
                             key_record_id=key_record_id, ttft_ms=ttft_ms,
                             cache_creation=cache_creation, cache_read=cache_read,
                             had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None)
        # v3.9.14 (#267 Phase 5.5 follow-up) — memory write-back for the
        # litellm streaming path. Assemble a non-streaming-shape response
        # dict from the accumulated tool_use blocks + feed through the
        # same maybe_extract_memory_writes the non-streaming and
        # claude-oauth paths use. Gated on (conversation_id, api_key_id);
        # silent-degrade so a memory store error never breaks the stream.
        if conversation_id and api_key_id and tool_calls_acc:
            try:
                content_list = []
                for tid, tc in tool_calls_acc.items():
                    raw = tc.get("input_str") or ""
                    try:
                        parsed = json.loads(raw) if raw else {}
                    except ValueError:
                        # malformed JSON — skip this block rather than
                        # corrupting the extractor's view of intent
                        continue
                    content_list.append({
                        "type": "tool_use",
                        "id": tc.get("id"),
                        "name": tc.get("name"),
                        "input": parsed,
                    })
                if content_list:
                    assembled = {
                        "id": "msg_proxy_stream",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": content_list,
                        "usage": {
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                        },
                    }
                    from app.memory.extract import maybe_extract_memory_writes
                    await maybe_extract_memory_writes(
                        db,
                        response_dict=assembled,
                        api_key_id=api_key_id,
                        conversation_id=conversation_id,
                        memory_tag_default=memory_tag,
                        source_provider_id=provider_id,
                    )
            except Exception:
                # Silent degrade — memory store errors never break the
                # stream's success path. Per the operator-locked design
                # (feedback_caller_memory_design_locked), all memory
                # side effects are best-effort.
                pass
        if cache_decision is not None and cache_decision.eligible:
            try:
                await maybe_store(cache_decision, "".join(full_text_buf))
            except Exception:
                pass
    except Exception as e:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=_exc_str(e),
                             had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None)
        yield (b'data: ' + json.dumps({"type": "error", "error": {"message": _exc_str(e)}}).encode() + b'\n\n')
        yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'


async def _webhook_completion_anthropic(
    webhook_url: str,
    model: str,
    messages: list,
    extra: dict,
    provider_id: str,
    db: AsyncSession,
    key_record_id: str,
    llm_hint: Optional[str] = None,  # v3.0.59
) -> None:
    """Run a non-streaming completion and POST the result to webhook_url."""
    t0 = time.monotonic()
    try:
        result = await acompletion_with_retry(model=model, messages=messages, stream=False, **extra)
        in_tok = getattr(result.usage, "prompt_tokens", 0)
        out_tok = getattr(result.usage, "completion_tokens", 0)
        cache_creation, cache_read = extract_cache_tokens(result.usage)
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id,
                             cache_creation=cache_creation, cache_read=cache_read,
                             had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None)
        await post_webhook(webhook_url, {
            "provider": provider_id,
            "model": model,
            "response": to_anthropic_response(result),
        })
    except Exception as exc:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=_exc_str(exc),
                             had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None)
        await post_webhook(webhook_url, {"error": _exc_str(exc), "model": model})


# ── Claude OAuth re-exports (v4.4.12 split) ──────────────────────────────────
# The claude-oauth dispatch helpers were extracted into a sibling module to
# keep this file under 1,000 LOC (pre-split: 979 LOC). Existing
# ``from app.api._messages_streaming import _stream_claude_oauth, ...``
# imports continue to work unchanged via these re-exports.
from app.api._messages_streaming_oauth import (  # noqa: E402
    _inject_claude_code_system,
    _count_cache_control_markers,
    _prepare_claude_oauth_request,
    _oauth_complete_timeout,
    _refresh_oauth_token,
    _complete_claude_oauth,
    _stream_claude_oauth,
    _CLAUDE_CODE_SYS_MARKER,
    _ALLOWED_SYS_MARKERS,
    _CLAUDE_OAUTH_TIMEOUT,
    _CLAUDE_OAUTH_STREAM_TIMEOUT,
)
