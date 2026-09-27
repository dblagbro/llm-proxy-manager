"""Response dispatch for /v1/messages — the streaming and non-streaming arms.

v5.22.37 — extracted from ``app/api/messages.py``, which had reached 1409 LOC.
``design.md`` rule 1 puts the split trigger at ~600 lines, and three pins in
``tests/unit/`` (v5.19.0, v5.7.18, v5.7.19) had been carrying successively
lower targets that nothing met; the binding one is <= 1080.

The seam is the ``if stream:`` / ``else:`` pair that was the final statement of
the handler's dispatch ``try`` block — 163 and 351 lines respectively. Both
arms always return or raise (verified before extracting), and nothing followed
the ``try``, so no state needs to flow back to the caller.

**Why a context object rather than keyword arguments.** The two arms needed 22
and 25 locals from the enclosing handler, **17 of them shared**. Two functions
with 22 and 25 keyword parameters would be less readable than the inline code
they replaced, which would defeat the point of the split; the shared-17 overlap
says these values travel together. ``MessagesDispatchCtx`` names that bundle
once. It follows the existing ``HookContext`` precedent in
``_response_hook_runner.py``.

The arm bodies are byte-identical to what they replaced, dedented and preceded
by an unpacking prologue. That was deliberate: a 500-line move is risky enough
without also rewriting the code, so each function starts by restoring exactly
the local names the original block ran under.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.api._messages_streaming import (
    _stream_anthropic,
    buffer_sse_until_content,
    http_status_for_stream_error,
    preflight_sse,
    stream_with_empty_guard,
)
from app.config import settings
from app.observability.prometheus import (
    observe_hedge_attempt,
    observe_hedge_bucket_reject,
    observe_hedge_win,
)
from app.routing.hedging import (
    race_streams,
    should_hedge_header,
    try_acquire_hedge,
    wait_budget_ms,
)
from app.routing.litellm_binding import clamp_thinking_budget
from app.routing.router import select_provider

logger = logging.getLogger(__name__)
router = APIRouter()


# pool-leak-audit: watchdog+bounded
# The watch_for_disconnect dep cancels the handler on client abort;
# LLM streams are bounded by upstream provider timeouts (~60s max).
# See v5.7.17 and CHANGELOG v5.21.8.
@router.post("/v1/messages")


@dataclass(frozen=True)
class MessagesDispatchCtx:
    """Everything the two dispatch arms read from the handler's scope.

    Frozen: the arms rebind some of these locally (``route`` on failover,
    ``messages_list`` on a retry) but nothing propagates back out, because both
    arms return. Rebinding a local is fine; mutating shared state would not be.
    """

    _buffered_cascade_stream: Any = None
    _compliance_disclosure: Any = None
    _compliance_wants_sse_prelude: Any = None
    _proxy_tools_injected: Any = None
    alias: Any = None
    anthropic_beta: Any = None
    background_tasks: Any = None
    body: Any = None
    cache_decision: Any = None
    db: Any = None
    extra: Any = None
    has_images: Any = None
    has_tools: Any = None
    hint: Any = None
    is_auto: Any = None
    key_record: Any = None
    litellm_tools: Any = None
    llm_hint: Any = None
    max_tokens: Any = None
    messages_list: Any = None
    parsed_slug: Any = None
    request: Any = None
    resp_headers: Any = None
    route: Any = None
    system: Any = None
    thinking: Any = None
    x_conversation_id: Any = None
    x_cot_cascade: Any = None
    x_hedge: Any = None
    x_memory_tag: Any = None


async def dispatch_streaming(ctx: MessagesDispatchCtx):
    """The ``if stream:`` arm, verbatim from messages.py:833-995."""
    _compliance_disclosure = ctx._compliance_disclosure
    _compliance_wants_sse_prelude = ctx._compliance_wants_sse_prelude
    alias = ctx.alias
    cache_decision = ctx.cache_decision
    db = ctx.db
    extra = ctx.extra
    has_images = ctx.has_images
    has_tools = ctx.has_tools
    hint = ctx.hint
    is_auto = ctx.is_auto
    key_record = ctx.key_record
    litellm_tools = ctx.litellm_tools
    llm_hint = ctx.llm_hint
    max_tokens = ctx.max_tokens
    messages_list = ctx.messages_list
    parsed_slug = ctx.parsed_slug
    resp_headers = ctx.resp_headers
    route = ctx.route
    system = ctx.system
    x_conversation_id = ctx.x_conversation_id
    x_hedge = ctx.x_hedge
    x_memory_tag = ctx.x_memory_tag

    lmrh_hedge = hint.get("hedge").value if (hint and hint.get("hedge")) else None
    wants_hedge = (
        settings.hedge_enabled
        and should_hedge_header(x_hedge, lmrh_hedge)
    )
    wait_ms = wait_budget_ms(route.provider.id) if wants_hedge else None

    if wait_ms is not None and await try_acquire_hedge():
        # Pick a backup provider (different from primary)
        try:
            backup_route = await select_provider(
                db, hint, has_tools=has_tools, has_images=has_images,
                key_type=key_record.key_type,
                exclude_provider_id=route.provider.id,
                excluded_provider_types={"claude-oauth"},
            )
        except Exception:
            backup_route = None
        # v5.22.4 — release the connection this backup re-select
        # opened before the hedged streams run for the whole request
        # (K1: was pinned across the stream). commit keeps route
        # attached (expire_on_commit=False).
        try:
            await db.commit()
        except Exception:
            pass

        if backup_route is not None:
            observe_hedge_attempt(route.provider.id, backup_route.provider.id)

            def _primary():
                return _stream_anthropic(
                    route.litellm_model, messages_list, extra, route.provider.id,
                    db, key_record.id, time.monotonic(), max_tokens,
                    cache_decision=cache_decision,
                    llm_hint=llm_hint,
                    api_key_id=key_record.id,
                    conversation_id=x_conversation_id,
                    memory_tag=x_memory_tag,
                    compliance_disclosure=_compliance_disclosure,
                    accept_compliance_events=_compliance_wants_sse_prelude,
                )

            def _backup():
                b_extra = {**backup_route.litellm_kwargs, "max_tokens": max_tokens}
                if system: b_extra["system"] = system
                if litellm_tools: b_extra["tools"] = litellm_tools
                if backup_route.native_thinking_params:
                    b_extra.update(backup_route.native_thinking_params)
                    # v5.3.7 — keep Gemini thinking budget below max_tokens (empty-success fix)
                    clamp_thinking_budget(b_extra)
                return _stream_anthropic(
                    backup_route.litellm_model, messages_list, b_extra,
                    backup_route.provider.id,
                    db, key_record.id, time.monotonic(), max_tokens,
                    cache_decision=None,  # don't store backup output under primary's key
                    llm_hint=llm_hint,
                    api_key_id=key_record.id,
                    conversation_id=x_conversation_id,
                    memory_tag=x_memory_tag,
                    compliance_disclosure=_compliance_disclosure,
                    accept_compliance_events=_compliance_wants_sse_prelude,
                )

            racer, winner = await race_streams(_primary, _backup, wait_ms)
            observe_hedge_win(winner)
            resp_headers["X-Hedged-Winner"] = winner
            # v3.10.16 BUG-001 — pre-flight the hedged stream too,
            # so a pre-stream upstream failure on the winning
            # branch surfaces as a real HTTP status instead of a
            # 200 + terminal SSE error frame (parity with the
            # non-hedged streaming path, fixed in v3.10.13).
            _hfirst, _herr, racer = await preflight_sse(racer)
            if _herr is not None:
                await racer.aclose()
                raise HTTPException(
                    http_status_for_stream_error(_herr),
                    f"Upstream error before streaming began: {_herr}",
                )

            # v5.3.9 — empty-success guard on the hedged winner;
            # on a content-free 200 stream record the breaker
            # failure and fall through to the guarded non-hedged
            # path below. (Mirrors completions.py.)
            _hframes, _h_has_content, racer = await buffer_sse_until_content(_hfirst, racer)
            if _h_has_content:
                async def _replay_hedged_stream(_fs=_hframes, _g=racer):
                    for _f in _fs:
                        yield _f
                    async for _c in _g:
                        yield _c

                return StreamingResponse(
                    _replay_hedged_stream(),
                    media_type="text/event-stream", headers=resp_headers,
                )
            await racer.aclose()
            from app.routing.circuit_breaker import record_failure as _rec_fail
            _dead_id = route.provider.id if winner == "primary" else backup_route.provider.id
            await _rec_fail(_dead_id, billing_error=False)
            logger.warning(
                "hedged stream empty-success (winner=%s provider=%s) — "
                "falling through to guarded non-hedged path", winner, _dead_id,
            )
    elif wait_ms is not None:
        observe_hedge_bucket_reject()

    # v3.10.13 BUG-001 — pre-flight the litellm streaming path so a
    # pre-stream upstream failure (auth, rate-limit, 5xx) surfaces
    # as a real HTTP status instead of a 200 + terminal SSE error
    # frame. Matches the claude-oauth streaming path, which already
    # pre-flights. A mid-stream failure (after message_start) still
    # degrades to an SSE error frame — the 200 is already sent.
    # v5.3.9 — wrapped in stream_with_empty_guard: a 200 +
    # content-free SSE stream (dead cursor-bridge pattern) records
    # a breaker failure and fails over instead of piping the
    # emptiness to the caller. (Mirrors completions.py.)
    def _start_anthropic_stream(_r):
        if _r is route:
            _e, _cd = extra, cache_decision
        else:
            _e = {**_r.litellm_kwargs, "max_tokens": max_tokens}
            if system: _e["system"] = system
            if litellm_tools: _e["tools"] = litellm_tools
            if _r.native_thinking_params:
                _e.update(_r.native_thinking_params)
                clamp_thinking_budget(_e)
            _cd = None  # don't store failover output under primary's key
        return _stream_anthropic(
            _r.litellm_model, messages_list, _e, _r.provider.id,
            db, key_record.id, time.monotonic(), max_tokens,
            cache_decision=_cd,
            llm_hint=llm_hint,
            api_key_id=key_record.id,
            conversation_id=x_conversation_id,
            memory_tag=x_memory_tag,
            compliance_disclosure=_compliance_disclosure,
            accept_compliance_events=_compliance_wants_sse_prelude,
        )

    from app.routing.aliases import is_logical_alias as _is_logical_alias
    _req_model = (alias.model_id if alias else parsed_slug.bare_model) or None
    _frames, _gen, _served_route = await stream_with_empty_guard(
        start_stream=_start_anthropic_stream, route=route, db=db,
        hint=hint, has_tools=has_tools, has_images=has_images,
        key_type=key_record.key_type, api_key_id=key_record.id,
        model_override=None if (is_auto or (alias is None and _is_logical_alias(_req_model))) else _req_model,
    )
    if _served_route is not route:
        resp_headers["X-Empty-Stream-Failover"] = "true"
        resp_headers["X-Empty-Stream-Failover-Target"] = _served_route.provider.provider_type

    async def _replay_anthropic_stream(_fs=_frames, _g=_gen):
        for _f in _fs:
            yield _f
        async for _c in _g:
            yield _c

    return StreamingResponse(
        _replay_anthropic_stream(),
        media_type="text/event-stream",
        headers=resp_headers,
    )


# ── non-streaming arm re-export (v5.22.37 split) ─────────────────────────────
# Moved to a sibling to keep this file clear of the 700-LOC ceiling. Imported
# here so ``messages.py`` — and the tests that read the handler surface — keep
# reaching all three names through one module. The import is at the BOTTOM
# because the sibling imports ``MessagesDispatchCtx`` from this module; at the
# top it would be a circular import.
from app.api._messages_nonstream_dispatch import (  # noqa: E402,F401
    dispatch_non_streaming,
)
