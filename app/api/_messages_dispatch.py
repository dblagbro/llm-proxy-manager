"""Dispatch orchestration for the /v1/messages endpoint.

Extracted from ``messages.py`` (v3.10.9) to keep the ``messages()``
handler legible. ``messages.py`` owns request preflight + routing + the
litellm / CoT path; this module owns the **claude-oauth provider-chain
walk** — the deepest, gnarliest branch of the old ~913-line handler.

Sibling split:
- ``_messages_streaming.py`` holds the SSE *generators*
  (``_stream_claude_oauth`` / ``_complete_claude_oauth`` / ``_stream_anthropic`` …).
- this file holds the *orchestration* that drives them — walking the
  claude-oauth chain, handling 401-refresh fallback, emitting the Response.

Behaviour is identical to the inline block it replaced; this is a pure
extraction.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from app.config import settings
from app.api._messages_streaming import _stream_claude_oauth, _complete_claude_oauth
from app.api._cache_inject import (
    inject_cache_control, parse_cache_mode, resolve_min_chars,
    build_cache_disclosure, append_cache_disclosure,
)
from app.api._quality_hint import merge_into_headers
from app.memory.extract import maybe_extract_memory_writes

logger = logging.getLogger(__name__)


async def _select_excluding(db, hint, has_tools, has_images, key_type, excluded: set[str], api_key_id=None):
    """v2.8.6: select_provider only accepts a single exclude_id. To walk
    through a chain of OAuth providers we need to call it repeatedly,
    excluding one id per pass and discarding any pick already in the
    tried set. Once we land on a never-tried provider, return its route.
    v3.0.45: forwards api_key_id for tenant scoping."""
    from app.routing.router import select_provider as _select
    last_exc = None
    # Cap iterations conservatively so we never spin if every provider was tried.
    for _ in range(20):
        # Pass any excluded id (select_provider only accepts one) and check
        # the chosen route. If it's still in `excluded`, expand the exclusion
        # set and retry.
        seed = next(iter(excluded), None) if excluded else None
        try:
            r = await _select(
                db, hint, has_tools=has_tools, has_images=has_images,
                key_type=key_type, exclude_provider_id=seed,
                api_key_id=api_key_id,
            )
        except Exception as e:
            last_exc = e
            break
        if r.provider.id in excluded:
            # The single-excluded select picked another tried provider —
            # add it to excluded and re-pick. This loop terminates because
            # `excluded` strictly grows and there are finitely many providers.
            excluded.add(r.provider.id)
            continue
        return r
    if last_exc:
        raise last_exc
    raise RuntimeError("All providers tried")


async def dispatch_claude_oauth_chain(
    route,
    *,
    body: dict,
    db,
    key_record,
    resp_headers: dict,
    stream: bool,
    max_tokens: int,
    llm_hint: Optional[str],
    hint,
    has_tools: bool,
    has_images: bool,
    conversation_id: Optional[str],
    memory_tag: Optional[str],
):
    """Walk the claude-oauth provider chain and dispatch the request.

    Returns ``(response, route)``:
      - ``response`` non-None → the request was served (StreamingResponse
        or JSONResponse); the caller returns it unchanged.
      - ``response`` is None → the chain is exhausted and ``route`` now
        points at a non-claude-oauth provider; the caller falls through
        to the litellm path with that route.

    claude-oauth short-circuits the rest of the pipeline (no CoT, no tool
    emulation, no fallback chains, no cascade) — Claude Pro Max
    subscriptions already run through Claude Code's server-side routing,
    so we just forward the raw /v1/messages body to platform.claude.com
    with the OAuth header bundle.

    v2.8.6: when claude-oauth fails over, the next-priority provider may
    ALSO be claude-oauth (e.g. Devin-VG → Devin-Gmail). We walk down the
    OAuth chain first; only after all OAuth options are exhausted does the
    request fall into the regular litellm path.
    """
    tried_oauth_ids: set[str] = set()
    while route.provider.provider_type == "claude-oauth":
        access_token = route.provider.api_key or ""
        t0 = time.monotonic()
        upstream_body = dict(body)
        # v3.0.42: auto-cache injection. Coordinator-hub bot daemons send
        # large stable system prompts repeatedly; the v3.0.39 audit showed
        # cache_control adoption at exactly 0% across 3,005 claude-oauth
        # events in 24h. Wrap the last system block with cache_control:
        # ephemeral when above threshold and the caller hasn't done so.
        # v3.0.69: LMRH 1.2 §E2 — ``cache=ephemeral`` forces inject even
        # below threshold; ``cache=none|off|disabled`` opts out.
        cache_decision = parse_cache_mode(llm_hint)
        cache_injected = False
        if cache_decision.mode != "none":
            upstream_body, cache_injected = inject_cache_control(
                upstream_body, "claude-oauth",
                min_chars=resolve_min_chars(cache_decision),
            )
        oauth_provider_id = route.provider.id
        tried_oauth_ids.add(oauth_provider_id)
        if stream:
            # v2.7.6: pre-flight the streaming connection so 401/4xx errors
            # become proper HTTP responses instead of SSE-error-then-200.
            # _stream_claude_oauth raises HTTPStatusError on pre-stream
            # failure (after one auto-refresh retry on 401).
            stream_gen = _stream_claude_oauth(
                access_token, upstream_body,
                provider_id=oauth_provider_id, db=db,
                key_record_id=key_record.id, t0=t0,
                budget_total=max_tokens,
                provider_name=route.provider.name,
                llm_hint=llm_hint,
                # v3.9.11 Phase 5.5 — pass conv/tag into stream so the
                # assembled response can be fed to maybe_extract_memory_writes
                # after the SSE terminates.
                api_key_id=key_record.id,
                conversation_id=conversation_id,
                memory_tag=memory_tag,
            )
            try:
                first_chunk = await stream_gen.__anext__()
            except httpx.HTTPStatusError as e:
                # v2.7.6 BUG-018: streaming has no failover (would break SSE
                # contract); surface the upstream error as HTTP status.
                raise HTTPException(
                    e.response.status_code,
                    f"Claude OAuth upstream: {e.response.text[:200] if e.response else str(e)}",
                )
            except httpx.HTTPError as e:
                raise HTTPException(502, f"Claude OAuth upstream: {e}")
            except StopAsyncIteration:
                raise HTTPException(502, "Claude OAuth upstream: empty stream")

            async def _replay():
                yield first_chunk
                async for c in stream_gen:
                    yield c

            resp_headers["X-Cache-Status"] = "bypass"
            return StreamingResponse(
                _replay(),
                media_type="text/event-stream",
                headers=resp_headers,
            ), route
        try:
            result = await _complete_claude_oauth(
                access_token, upstream_body,
                provider_id=oauth_provider_id, db=db,
                key_record_id=key_record.id, t0=t0,
                provider_name=route.provider.name,
                llm_hint=llm_hint,
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 502
            if settings.fallback_enabled and status in (401, 403):
                logger.info(
                    f"claude-oauth provider {oauth_provider_id} returned {status} after refresh; "
                    f"trying next provider (already tried oauth ids: {tried_oauth_ids})"
                )
                try:
                    # v2.8.6: exclude EVERY already-tried OAuth provider so we
                    # walk through the OAuth chain instead of bouncing back to
                    # the same one. select_provider only takes one exclude_id;
                    # repeat-call until we get one we haven't tried.
                    route = await _select_excluding(
                        db, hint, has_tools, has_images, key_record.key_type, tried_oauth_ids,
                    )
                except Exception as sel_exc:
                    logger.warning(f"no fallback provider available: {sel_exc}")
                    raise HTTPException(status, f"Claude OAuth upstream: {e.response.text[:200]}")
                resp_headers["X-Fallback-From"] = "claude-oauth"
                # Continue the while-loop: if the new route is also claude-oauth,
                # we re-enter the OAuth dispatch; otherwise fall out into litellm.
                continue
            raise HTTPException(status, f"Claude OAuth upstream: {e.response.text[:200]}")
        except httpx.HTTPError as e:
            if settings.fallback_enabled:
                logger.info(f"claude-oauth provider {oauth_provider_id} network error; trying next provider")
                try:
                    route = await _select_excluding(
                        db, hint, has_tools, has_images, key_record.key_type, tried_oauth_ids,
                    )
                except Exception:
                    raise HTTPException(502, f"Claude OAuth upstream: {e}")
                resp_headers["X-Fallback-From"] = "claude-oauth"
                continue
            raise HTTPException(502, f"Claude OAuth upstream: {e}")
        else:
            resp_headers["X-Cache-Status"] = "bypass"
            # v3.0.83/.85 disclosure refactored to a shared helper in
            # v3.0.87 — handles cache=, cache-injected=?1, cache-tokens-
            # read/written, and the cross-family-substitution
            # cache=ignored case in one place.
            append_cache_disclosure(
                resp_headers,
                build_cache_disclosure(
                    llm_hint=llm_hint,
                    cache_decision=cache_decision,
                    cache_injected=cache_injected,
                    served_provider_type=route.provider.provider_type,
                    usage=(result or {}).get("usage"),
                ),
            )
            # v3.6.1 — merge X-Quality-Hint header if response looks thin
            merge_into_headers(resp_headers, result, endpoint="messages")
            # v3.9.0 (#267) Phase 5 — memory-tool write-back. Scans the
            # Anthropic response for memory tool_use blocks and persists
            # writes to the king-store. No-op unless caller_memory_enabled
            # AND X-Conversation-Id is set. Silent degrade on any error.
            mem_writes = await maybe_extract_memory_writes(
                db, response_dict=result,
                api_key_id=key_record.id,
                conversation_id=conversation_id,
                memory_tag_default=memory_tag,
                source_provider_id=oauth_provider_id,
            )
            if mem_writes:
                resp_headers["X-Caller-Memory-Writes"] = str(mem_writes)
            return JSONResponse(content=result, headers=resp_headers), route
        # Defensive: should be unreachable — every branch above either returned,
        # raised, or continued. Break to avoid an accidental infinite loop.
        break

    # Chain exhausted (or never entered) — route now points at a
    # non-claude-oauth provider; caller falls through to the litellm path.
    return None, route
