"""Claude OAuth (v2.7.0) streaming + non-streaming dispatch.

Extracted from ``_messages_streaming.py`` in v4.4.12 to keep the
parent file under 1,000 LOC. Direct httpx handlers for
``provider_type="claude-oauth"`` — bypasses litellm because (a)
``platform.claude.com`` uses ``Authorization: Bearer`` (not
``x-api-key``) and (b) the response is already in Anthropic's
native format, so we can forward the stream/body verbatim without
going through any adapter.

Public surface (re-exported from ``_messages_streaming.py``):
- ``_inject_claude_code_system`` — required system-prompt prefix
- ``_complete_claude_oauth`` — non-streaming dispatch
- ``_stream_claude_oauth`` — streaming dispatch
"""
from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.middleware import maybe_store
from app.monitoring.helpers import record_outcome
from app.providers.claude_oauth import build_headers as _claude_oauth_headers, PLATFORM_BASE_URL

logger = logging.getLogger(__name__)


def _exc_str(e: BaseException) -> str:
    """v2.8.10: produce a non-empty error string. Duplicate of the
    canonical version in ``_messages_streaming.py`` — kept local to
    avoid a circular import with the re-export shim. The two copies
    are intentionally identical; if one is updated, update the
    other (test_v4412_streaming_split.py asserts equality)."""
    s = str(e) if e else ""
    return s if s else f"{type(e).__name__} (no message)"


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
    # v4.4.29 (BUG-085) — when the caller already sent more than 4
    # cache_control markers, log the breakdown so we can tell from the
    # logs whether the 400 is hub-side or proxy-side. The proxy never
    # adds a 5th itself (the `< 4` gate below stops it from injecting
    # cache_control when the count is already 4+), so >4 means the
    # CALLER sent the excess. Without this log, an operator only sees
    # "Found 5" in the upstream error text and has to body-sample to
    # diagnose. Surfaced 2026-05-29 on coordinator-hub (14 such errors
    # in 24h).
    cc_count = _count_cache_control_markers(body)
    if cc_count > 4:
        try:
            sys_cc = sum(
                1 for b in (sys_field if isinstance(sys_field, list) else [])
                if isinstance(b, dict) and b.get("cache_control")
            )
            msg_cc = sum(
                1
                for msg in (body.get("messages") or [])
                if isinstance(msg.get("content") if isinstance(msg, dict) else None, list)
                for blk in (msg.get("content") or [])
                if isinstance(blk, dict) and blk.get("cache_control")
            )
            tool_cc = sum(
                1 for t in (body.get("tools") or [])
                if isinstance(t, dict) and t.get("cache_control")
            )
            logger.warning(
                "claude-oauth: caller sent %d cache_control markers "
                "(Anthropic caps at 4 — upstream will 400). "
                "breakdown: sys=%d msgs=%d tools=%d. "
                "Proxy did NOT add its own marker's cache_control.",
                cc_count, sys_cc, msg_cc, tool_cc,
            )
        except Exception:
            pass  # never break the dispatch path on telemetry
    if not sys_already_cached and cc_count < 4:
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
# the 2026-05-05 internet outage).
#
# Non-streaming `read` is effectively the whole-generation budget: the
# response body arrives only after Claude finishes generating, so a
# large Pro Max generation legitimately needs minutes — kept at 300s.
_CLAUDE_OAUTH_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)
# v3.10.12 BUG-037 — streaming `read` is the gap BETWEEN chunks, not the
# total: a healthy SSE stream emits tokens continuously and never pauses
# this long. A 300s per-chunk gap only ever means a hung upstream, so a
# tight streaming ceiling bounds the hang (a hung request also pins a DB
# connection — an ARCH-A pool-leak contributor) without cutting any real
# stream.
_CLAUDE_OAUTH_STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)


def _oauth_complete_timeout(max_tokens) -> httpx.Timeout:
    """v3.10.13 BUG-037 — for the NON-streaming claude-oauth call, `read`
    is effectively the whole-generation budget (the body arrives only
    once Claude finishes). Scale it to the request's ``max_tokens``
    instead of a flat 300s: a tiny request — e.g. an unregistered model
    id that routes here and hangs — is bounded to ~90s, while a genuinely
    large generation still gets up to the 300s ceiling. Bounds the
    BUG-037 hang with no risk to real large completions."""
    ceiling = _CLAUDE_OAUTH_TIMEOUT.read or 300.0
    try:
        budget = 90.0 + float(max_tokens or 0) * 0.035
    except (TypeError, ValueError):
        budget = ceiling
    read = max(90.0, min(budget, ceiling))
    return httpx.Timeout(connect=5.0, read=read, write=10.0, pool=5.0)


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
                timeout=_oauth_complete_timeout(body.get("max_tokens", 4096)),
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
                timeout=_CLAUDE_OAUTH_STREAM_TIMEOUT,
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
