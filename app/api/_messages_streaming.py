"""
Tail functions for /v1/messages (Anthropic endpoint).

Extracted from ``app/api/messages.py`` in the 2026-04-23 refactor so the
POST handler can stay focused on routing + response assembly.

Functions:
  _stream_cot_anthropic          — pass-through around run_cot_pipeline + metrics
  _stream_anthropic              — the main Anthropic streaming translator (litellm path)
  _stream_claude_oauth           — direct httpx stream to platform.claude.com (v2.7.0)
  _complete_claude_oauth         — non-streaming equivalent (v2.7.0)
  _webhook_completion_anthropic  — fire-and-forget async delivery
"""
from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator, Optional

import httpx
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
) -> AsyncIterator[bytes]:
    """Pass-through wrapper around run_cot_pipeline; records metrics after completion."""
    import json as _json
    in_tok = out_tok = 0
    cache_creation = cache_read = 0
    t0 = time.monotonic()
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
) -> AsyncIterator[bytes]:
    try:
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

        if text_started or tool_started:
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
        if budget_total > 0:
            remaining = max(0, budget_total - output_tokens)
            yield (
                f'event: budget\ndata: {{"remaining":{remaining},'
                f'"used":{output_tokens},"total":{budget_total}}}\n\n'
            ).encode()
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


# ── Claude OAuth (v2.7.0) ────────────────────────────────────────────────────
# Direct httpx handlers for provider_type="claude-oauth". We bypass litellm
# because (a) platform.claude.com uses Authorization: Bearer (not x-api-key)
# and (b) the response is already in Anthropic's native format, so we can
# forward the stream/body verbatim without going through any adapter.

# v2.7.2: Anthropic's OAuth-auth'd /v1/messages endpoint requires the system
# prompt to START with the Claude Code marker. Without it the API returns a
# masked ``rate_limit_error`` with message ``"Error"`` regardless of the real
# rejection reason, making it impossible to debug. The CLI hardcodes these
# same three variants — we always prepend the base variant; if the caller's
# own system block already starts with any allowed marker we leave it alone.
_CLAUDE_CODE_SYS_MARKER = "You are Claude Code, Anthropic's official CLI for Claude."
_ALLOWED_SYS_MARKERS = (
    _CLAUDE_CODE_SYS_MARKER,
    _CLAUDE_CODE_SYS_MARKER + ", running within the Claude Agent SDK.",
    "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
)


def _count_cache_control_markers(body: dict) -> int:
    """v2.8.9: count cache_control markers across system, messages, and tools.
    Anthropic caps the total at 4 — when the caller already has 4, we must
    inject the CC marker WITHOUT cache_control to avoid a 400."""
    n = 0
    sys_field = body.get("system")
    if isinstance(sys_field, list):
        for b in sys_field:
            if isinstance(b, dict) and b.get("cache_control"):
                n += 1
    for msg in (body.get("messages") or []):
        c = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("cache_control"):
                    n += 1
    for tool in (body.get("tools") or []):
        if isinstance(tool, dict) and tool.get("cache_control"):
            n += 1
    return n


def _inject_claude_code_system(body: dict) -> dict:
    """Ensure the outgoing body's ``system`` starts with the CC marker.

    Returns a shallow-copied body; callers pass the result to httpx.
    """
    sys_field = body.get("system")

    def _first_text(v) -> str:
        if isinstance(v, str):
            return v
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, dict) and first.get("type") == "text":
                return str(first.get("text") or "")
        return ""

    head = _first_text(sys_field).lstrip()
    if head and any(head.startswith(m) for m in _ALLOWED_SYS_MARKERS):
        return body  # caller already identifying as Claude Code

    # v2.7.6 BUG-006: marker block carries cache_control so the prefix stays
    # in Anthropic's prompt cache across calls. Without this, a non-cacheable
    # block at index 0 would shift the cache key on every request.
    # v2.8.9: Anthropic caps cache_control markers at 4 per request. If the
    # caller already has 4, omit ours to avoid a 400.
    # v3.0.54: when the caller already attaches cache_control to a downstream
    # system block, DON'T add a second breakpoint to the marker. Two
    # breakpoints where breakpoint 1 is below the per-model token minimum
    # (~14 marker tokens vs ~1024-4096 minimum) creates a sub-threshold
    # cache attempt that returns cache_creation=cache_read=0 and may, in
    # some upstream behaviors, suppress the second breakpoint's caching as
    # well. Single-breakpoint mode is unambiguously cacheable: marker text
    # stays anchored at the prefix start (so the cache key is stable) but
    # only the caller's larger downstream block defines the breakpoint.
    sys_already_cached = isinstance(sys_field, list) and any(
        isinstance(b, dict) and "cache_control" in b for b in sys_field
    )
    marker_block: dict = {"type": "text", "text": _CLAUDE_CODE_SYS_MARKER}
    if not sys_already_cached and _count_cache_control_markers(body) < 4:
        marker_block["cache_control"] = {"type": "ephemeral"}

    if sys_field is None:
        new_system: list | str = [marker_block]
    elif isinstance(sys_field, str):
        # Preserve caller's system as a second block rather than prefix-joining
        # so the marker stays isolated (CC's real format).
        new_system = [marker_block, {"type": "text", "text": sys_field}]
    elif isinstance(sys_field, list):
        new_system = [marker_block, *sys_field]
    else:
        new_system = [marker_block]

    return {**body, "system": new_system}


# v3.5.5 R4 — shared httpx timeout for both claude-oauth dispatch paths.
# Single source of truth; previously declared verbatim inside each of
# _complete_claude_oauth + _stream_claude_oauth. The split connect /
# read / write / pool config dates from v3.0.60 (single timeout=300
# meant DNS / TCP-connect failures held the request for 300s while
# upstream was confirmed dead — exhausted the SQLAlchemy pool during
# the 2026-05-05 internet outage). Read budget is generous because
# Pro Max subscription stream responses can take a few minutes for
# long context windows.
_CLAUDE_OAUTH_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)


def _prepare_claude_oauth_request(body: dict, *, stream: bool) -> tuple[str, dict]:
    """v3.5.5 R4 — pre-flight setup for a claude-oauth dispatch call.

    Returns ``(url, prepared_body)`` ready to feed to httpx.

    Pre-R4 each of ``_complete_claude_oauth`` + ``_stream_claude_oauth``
    re-declared:

        url = f"{PLATFORM_BASE_URL}/v1/messages?beta=true"
        body = {**body}                       # (or {**body, "stream": True} for streams)
        body.setdefault("max_tokens", 4096)
        body = _inject_claude_code_system(body)

    Now one helper. Future changes to Anthropic's URL conventions, beta
    header layout, or the max_tokens default land in one place.

    ``stream=True`` adds ``"stream": True`` to the body up-front so the
    SSE path doesn't have to merge twice; ``stream=False`` returns the
    body unchanged from the merge.
    """
    url = f"{PLATFORM_BASE_URL}/v1/messages?beta=true"
    prepared = {**body, "stream": True} if stream else {**body}
    prepared.setdefault("max_tokens", 4096)
    prepared = _inject_claude_code_system(prepared)
    return url, prepared


async def _refresh_oauth_token(provider_id: str, db: AsyncSession) -> Optional[str]:
    """Fetch the provider, run refresh_and_persist, return new access_token.
    Returns None if refresh fails (e.g. invalid_grant — admin must re-auth)."""
    from sqlalchemy import select
    from app.models.db import Provider
    from app.providers.claude_oauth_flow import refresh_and_persist, OAuthFlowError
    try:
        r = await db.execute(select(Provider).where(Provider.id == provider_id))
        provider = r.scalar_one_or_none()
        if provider is None or not provider.oauth_refresh_token:
            return None
        result = await refresh_and_persist(provider, db)
        logger.info(f"claude-oauth provider {provider_id}: token refreshed via 401-retry")
        return result.access_token
    except OAuthFlowError as e:
        logger.warning(f"claude-oauth provider {provider_id}: refresh failed: {e}")
        return None
    except Exception as e:
        logger.exception(f"claude-oauth provider {provider_id}: refresh raised: {e}")
        return None


async def _complete_claude_oauth(
    access_token: str,
    body: dict,
    provider_id: str,
    db: AsyncSession,
    key_record_id: str,
    t0: float,
    provider_name: Optional[str] = None,
    # v3.0.58: plumb the raw LLM-Hint header through claude-oauth so
    # event_meta.lmrh_hint captures it (the claude-oauth dispatch path
    # was uninstrumented in v3.0.55, leaving paperless's hint invisible
    # in activity logs even though FastAPI parsed it correctly).
    llm_hint: Optional[str] = None,
) -> dict:
    """Non-streaming ``/v1/messages`` call against platform.claude.com.

    Auto-refreshes the access_token on 401 and retries once. If the second
    attempt also fails or refresh fails (e.g. revoked refresh_token), the
    underlying httpx.HTTPStatusError propagates so the dispatch in
    messages.py converts it to an HTTP error response.

    v2.8.9: defaults ``max_tokens`` to 4096 if absent — Anthropic's API
    requires it and otherwise returns a confusing 400.
    """
    # v3.5.5 R4: URL + body prep + timeout extracted to module-level
    # helpers (single source of truth; the split-timeout rationale lives
    # in the _CLAUDE_OAUTH_TIMEOUT constant docstring).
    url, body = _prepare_claude_oauth_request(body, stream=False)
    current_token = access_token
    refreshed = False

    while True:
        headers = {
            **_claude_oauth_headers(current_token, model=body.get("model")),
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=_CLAUDE_OAUTH_TIMEOUT,
                follow_redirects=True,
            ) as client:
                r = await client.post(url, json=body, headers=headers)
            if r.status_code == 401 and not refreshed:
                # One-shot refresh-and-retry
                new_token = await _refresh_oauth_token(provider_id, db)
                if new_token:
                    current_token = new_token
                    refreshed = True
                    continue
                # Fall through to error path
            # v3.0.43: requested_model = body model (caller's value).
            req_model = body.get("model") or "claude-oauth"
            if r.status_code >= 400:
                await record_outcome(
                    db, provider_id, req_model, success=False,
                    key_record_id=key_record_id, error_str=f"{r.status_code}: {r.text[:200]}",
                    provider_name=provider_name, request_body=body,
                    requested_model=req_model,
                    had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None,
                )
                r.raise_for_status()
            data = r.json()
            usage = data.get("usage") or {}
            in_tok = int(usage.get("input_tokens") or 0)
            out_tok = int(usage.get("output_tokens") or 0)
            cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            # v3.0.43: also surface served_model from upstream's response.
            served_actual = data.get("model") or req_model
            await record_outcome(
                db, provider_id, served_actual, success=True,
                in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id,
                cache_creation=cache_creation, cache_read=cache_read,
                provider_name=provider_name,
                request_body=body, response_body=data,
                requested_model=req_model,
                had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None,
            )
            return data
        except httpx.HTTPError as e:
            await record_outcome(
                db, provider_id, body.get("model") or "claude-oauth", success=False,
                key_record_id=key_record_id, error_str=_exc_str(e),
                provider_name=provider_name, request_body=body,
                requested_model=body.get("model") or "claude-oauth",
                had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None,
            )
            raise


async def _stream_claude_oauth(
    access_token: str,
    body: dict,
    provider_id: str,
    db: AsyncSession,
    key_record_id: str,
    t0: float,
    budget_total: int = 0,
    cache_decision=None,
    provider_name: Optional[str] = None,
    llm_hint: Optional[str] = None,  # v3.0.58: capture in event_meta.lmrh_hint
    # v3.9.11 Phase 5.5 — caller-memory write-back for streamed responses.
    # When ``conversation_id`` is set, the assembled response_dict
    # (containing tool_use blocks for memory_20250818) gets fed through
    # the same maybe_extract_memory_writes path as non-streaming. No-op
    # when conversation_id is None (legacy non-memory traffic).
    api_key_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    memory_tag: Optional[str] = None,
) -> AsyncIterator[bytes]:
    """Streaming ``/v1/messages`` — platform.claude.com already emits
    Anthropic SSE, so we can forward chunks as-is and just sniff usage
    events for metrics + cache storage.

    Pre-stream errors (401, 4xx, network failure before the first byte) raise
    ``httpx.HTTPStatusError`` so the dispatch in messages.py can convert to a
    proper HTTP error response — never yields a fake ``message_stop``. On 401
    we run ``refresh_and_persist`` and retry the connection once before
    surfacing the error.

    Mid-stream errors (after the first byte) emit an SSE ``error`` event +
    ``[DONE]`` but do NOT synthesize ``message_stop``: the stream is broken,
    not complete, and clients must distinguish the two.
    """
    # v3.5.5 R4: URL + body prep + timeout extracted (same helpers as
    # _complete_claude_oauth; ``stream=True`` adds the SSE flag to body).
    url, body = _prepare_claude_oauth_request(body, stream=True)

    in_tok = out_tok = 0
    cache_creation = cache_read = 0
    ttft_ms: float = 0.0
    full_text_buf: list[str] = []
    # v2.8.4: assemble a synthetic response_body matching the non-streaming
    # shape so the activity log shows tool calls, content blocks, etc.
    assembled_blocks: dict[int, dict] = {}  # index → {type, text|input, ...}
    assembled_meta: dict = {}  # message_start metadata

    current_token = access_token
    refreshed = False
    yielded_first_chunk = False

    while True:
        headers = {
            **_claude_oauth_headers(current_token, model=body.get("model")),
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=_CLAUDE_OAUTH_TIMEOUT,
                follow_redirects=True,
            ) as client:
                async with client.stream("POST", url, json=body, headers=headers) as r:
                    # Pre-stream error handling (no bytes yielded yet)
                    if r.status_code == 401 and not refreshed:
                        await r.aread()  # drain
                        new_token = await _refresh_oauth_token(provider_id, db)
                        if new_token:
                            current_token = new_token
                            refreshed = True
                            # Restart the loop with the new token
                            continue
                        # Fall through to generic error path below
                    if r.status_code >= 400:
                        err_body = (await r.aread()).decode(errors="replace")[:400]
                        await record_outcome(
                            db, provider_id, body.get("model") or "claude-oauth", success=False,
                            key_record_id=key_record_id,
                            error_str=f"{r.status_code}: {err_body}",
                            provider_name=provider_name, request_body=body,
                            had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None,
                        )
                        # RAISE — dispatch will convert to HTTP error response.
                        # Do NOT yield SSE error frames; status hasn't been sent.
                        raise httpx.HTTPStatusError(
                            f"{r.status_code}: {err_body}", request=r.request, response=r,
                        )

                    # 2xx — start streaming bytes
                    async for chunk in r.aiter_bytes():
                        if not chunk:
                            continue
                        if not yielded_first_chunk:
                            ttft_ms = (time.monotonic() - t0) * 1000
                            yielded_first_chunk = True
                        yield chunk
                        # Parse SSE events for usage + full text
                        for line in chunk.decode(errors="replace").splitlines():
                            if not line.startswith("data: "):
                                continue
                            payload = line[6:].strip()
                            if not payload or payload == "[DONE]":
                                continue
                            try:
                                evt = json.loads(payload)
                            except ValueError:
                                continue
                            t = evt.get("type")
                            if t == "message_start":
                                msg = evt.get("message") or {}
                                usage = msg.get("usage") or {}
                                in_tok = int(usage.get("input_tokens") or in_tok)
                                cache_creation = int(usage.get("cache_creation_input_tokens") or cache_creation)
                                cache_read = int(usage.get("cache_read_input_tokens") or cache_read)
                                # v2.8.4: capture top-level message metadata for synthesis
                                assembled_meta = {
                                    k: v for k, v in msg.items()
                                    if k in ("id", "model", "role", "type", "stop_reason", "stop_sequence")
                                }
                            elif t == "message_delta":
                                usage = evt.get("usage") or {}
                                out_tok = int(usage.get("output_tokens") or out_tok)
                                delta = evt.get("delta") or {}
                                if "stop_reason" in delta:
                                    assembled_meta["stop_reason"] = delta["stop_reason"]
                                if "stop_sequence" in delta:
                                    assembled_meta["stop_sequence"] = delta["stop_sequence"]
                            elif t == "content_block_start":
                                idx = evt.get("index", 0)
                                cb = evt.get("content_block") or {}
                                # Initialize assembled block — text/tool_use/etc.
                                if cb.get("type") == "tool_use":
                                    assembled_blocks[idx] = {
                                        "type": "tool_use",
                                        "id": cb.get("id"),
                                        "name": cb.get("name"),
                                        "input": "",  # filled by input_json_delta
                                    }
                                elif cb.get("type") == "thinking":
                                    assembled_blocks[idx] = {"type": "thinking", "thinking": ""}
                                else:
                                    assembled_blocks[idx] = {"type": cb.get("type", "text"), "text": ""}
                            elif t == "content_block_delta":
                                idx = evt.get("index", 0)
                                delta = evt.get("delta") or {}
                                blk = assembled_blocks.setdefault(idx, {"type": "text", "text": ""})
                                if delta.get("type") == "text_delta":
                                    txt = delta.get("text") or ""
                                    full_text_buf.append(txt)
                                    blk["text"] = (blk.get("text") or "") + txt
                                elif delta.get("type") == "thinking_delta":
                                    blk["thinking"] = (blk.get("thinking") or "") + (delta.get("thinking") or "")
                                elif delta.get("type") == "input_json_delta":
                                    # tool_use input streams as partial JSON
                                    blk["input"] = (blk.get("input") or "") + (delta.get("partial_json") or "")

            # v2.8.4: assemble final response body in non-streaming shape so
            # the activity log shows the actual content + tool calls.
            content_list = []
            for idx in sorted(assembled_blocks.keys()):
                blk = assembled_blocks[idx]
                if blk.get("type") == "tool_use":
                    raw_input = blk.get("input") or ""
                    try:
                        parsed_input = json.loads(raw_input) if raw_input else {}
                    except ValueError:
                        parsed_input = {"_raw": raw_input}
                    content_list.append({
                        "type": "tool_use", "id": blk.get("id"),
                        "name": blk.get("name"), "input": parsed_input,
                    })
                elif blk.get("type") == "thinking":
                    content_list.append({"type": "thinking", "thinking": blk.get("thinking", "")})
                else:
                    content_list.append({"type": blk.get("type", "text"), "text": blk.get("text", "")})
            assembled_response = {
                **assembled_meta,
                "content": content_list,
                "usage": {
                    "input_tokens": in_tok, "output_tokens": out_tok,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                },
            }

            # Successful end of stream
            if budget_total > 0:
                remaining = max(0, budget_total - out_tok)
                yield (
                    f'event: budget\ndata: {{"remaining":{remaining},'
                    f'"used":{out_tok},"total":{budget_total}}}\n\n'
                ).encode()
            # v3.0.43: requested_model + served-from-upstream-when-available
            req_model_str = body.get("model") or "claude-oauth"
            served_str = (assembled_response.get("model")
                          if isinstance(assembled_response, dict) and assembled_response.get("model")
                          else req_model_str)
            await record_outcome(
                db, provider_id, served_str, success=True,
                in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id,
                ttft_ms=ttft_ms, cache_creation=cache_creation, cache_read=cache_read,
                provider_name=provider_name,
                request_body=body, response_body=assembled_response,
                requested_model=req_model_str,
                had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None,
            )
            # v3.9.11 (#267) Phase 5.5 — feed the assembled response_dict
            # through the same memory-tool write-back path as the
            # non-streaming branch. assembled_response is shaped exactly
            # like a non-streaming /v1/messages response (top-level
            # content[] with tool_use blocks already JSON-parsed), so
            # the extractor processes it identically. Gated on
            # conversation_id presence (caller opted in to memory).
            if conversation_id and api_key_id:
                try:
                    from app.memory.extract import maybe_extract_memory_writes
                    await maybe_extract_memory_writes(
                        db,
                        response_dict=assembled_response,
                        api_key_id=api_key_id,
                        conversation_id=conversation_id,
                        memory_tag_default=memory_tag,
                        source_provider_id=provider_id,
                    )
                except Exception:
                    # Silent degrade — never break the stream's success path
                    pass
            if cache_decision is not None and cache_decision.eligible:
                try:
                    await maybe_store(cache_decision, "".join(full_text_buf))
                except Exception:
                    pass
            return
        except httpx.HTTPStatusError:
            # Pre-stream — propagate to dispatch (will become HTTP error)
            raise
        except httpx.HTTPError as e:
            await record_outcome(
                db, provider_id, body.get("model") or "claude-oauth", success=False,
                key_record_id=key_record_id, error_str=_exc_str(e),
                provider_name=provider_name, request_body=body,
                had_lmrh_hint=bool(llm_hint), lmrh_hint_raw=llm_hint or None,
            )
            if not yielded_first_chunk:
                # Pre-stream connection error — surface as HTTP error
                raise
            # Mid-stream — emit SSE error event + [DONE], NOT message_stop
            yield (
                b'event: error\ndata: '
                + json.dumps({"type": "error", "error": {"message": _exc_str(e)}}).encode()
                + b'\n\ndata: [DONE]\n\n'
            )
            return
