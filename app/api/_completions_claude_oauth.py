"""claude-oauth dispatch for /v1/chat/completions.

v5.22.37 — extracted from ``app/api/completions.py``, which was 1008 LOC
against the 900-LOC pin in ``test_v5723_phase2_shared_helpers``. That handler is
a single 943-line function, so the seam had to come from inside it; this branch
was the best candidate because it always returns, so nothing flows back to the
caller.

Follows the shape already used for the sibling own-dispatcher branches in the
same handler — ``_codex_oauth_dispatch.dispatch_codex_oauth`` and
``_grok_web_dispatch.dispatch_grok_web_openai`` — so all three now read the same
way at the call site instead of one being inline at four times the length.

The body is byte-identical to the branch it replaces. Every parameter is named
exactly as the local it stood in for, so the moved code needed no edits at all.

Original rationale, preserved verbatim:

    # v3.0.38: claude-oauth on /v1/chat/completions via OpenAI↔Anthropic
    # wire-format translation. DevinGPT ask 2026-05-01: their stack speaks
    # OpenAI ChatCompletion only; this lets them reach Devin-Anthropic-Max-VG
    # without a 600-LOC client-side branch for /v1/messages.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter()


# pool-leak-audit: watchdog+bounded
# Same rationale as app/api/messages.py::messages — the disconnect
# watchdog releases the session on abort; LLM streams are bounded
# (~60s max) so the session isn't held for the multi-hour class of
# leak that v5.21.7 fixed in runs.py.
@router.post("/v1/chat/completions")


async def dispatch_claude_oauth_openai(
    *,
    route,
    body: dict,
    stream: bool,
    db,
    key_record,
    llm_hint,
    resp_headers: dict,
    x_conversation_id,
    x_memory_tag,
):
    """Serve a claude-oauth provider on the OpenAI Chat Completions wire.

    Always returns a response — ``StreamingResponse`` when ``stream``, else
    ``JSONResponse``. Never falls through.
    """
    from app.api._cache_inject import (
        inject_cache_control,
        parse_cache_mode,
        resolve_min_chars,
    )
    from app.api._messages_streaming import (
        _complete_claude_oauth,
        _stream_claude_oauth,
    )
    from app.api._oauth_chat_translate import (
        anthropic_response_to_openai,
        openai_request_to_anthropic,
        stream_anthropic_to_openai_sse,
    )
    t0 = time.monotonic()
    anthropic_body = openai_request_to_anthropic(body)
    # v3.0.42: auto-cache injection on the OpenAI-→-Anthropic
    # translation path too. v3.0.69: full LMRH 1.2 §E2 mode parsing.
    cache_decision = parse_cache_mode(llm_hint)
    cache_injected = False
    if cache_decision.mode != "none":
        anthropic_body, cache_injected = inject_cache_control(
            anthropic_body, "claude-oauth",
            min_chars=resolve_min_chars(cache_decision),
        )
    # Resolve the actual model the caller asked for; the routing layer
    # may have substituted a default model on cross-family fallback,
    # but for claude-oauth same-family we want the caller's value.
    if route.cross_family_fallback and route.served_model_native:
        anthropic_body["model"] = route.served_model_native
    # v5.0.21 — per-provider 1M-context opt-out via ContextVar.
    # Same pattern as _messages_dispatch.py — set before invoking
    # the OAuth path so build_headers strips the long-context beta.
    # v5.0.21 hotfix: defensive getattr + identity-check for bool.
    from app.providers.claude_oauth import set_disable_long_context
    set_disable_long_context(
        (getattr(route.provider, "extra_config", None) or {}).get("disable_long_context") is True
    )
    # Override stream flag from the request body so `stream=True` propagates.
    # v3.0.40: removed the inline imports — they triggered Python's
    # "import binds the name as local in the enclosing function" rule,
    # which made the module-level JSONResponse/StreamingResponse refs
    # in the OpenAI fallthrough path raise UnboundLocalError. Surfaced
    # in the v3.0.39 24h audit as 41+1 errors on the OpenAI provider.
    if stream:
        anthropic_sse = _stream_claude_oauth(
            access_token=route.provider.api_key,
            body=anthropic_body,
            provider_id=route.provider.id,
            db=db,
            key_record_id=key_record.id,
            t0=t0,
            provider_name=route.provider.name,
            llm_hint=llm_hint,
            # v3.9.11 Phase 5.5 — pass conv/tag through for stream
            # memory write-back. Same shape as messages.py path.
            api_key_id=key_record.id,
            conversation_id=x_conversation_id,
            memory_tag=x_memory_tag,
        )
        openai_sse = stream_anthropic_to_openai_sse(
            anthropic_sse, requested_model=body.get("model") or "",
        )
        return StreamingResponse(openai_sse, media_type="text/event-stream",
                                  headers=resp_headers)
    else:
        anth_resp = await _complete_claude_oauth(
            access_token=route.provider.api_key,
            body=anthropic_body,
            provider_id=route.provider.id,
            db=db,
            key_record_id=key_record.id,
            t0=t0,
            provider_name=route.provider.name,
            llm_hint=llm_hint,
        )
        # v3.0.83/.84/.85 disclosure refactored to a shared helper
        # in v3.0.87 — handles cache=, cache-injected, cache-tokens-
        # read/written, and the cross-family-substitution
        # cache=ignored case in one place.
        from app.api._cache_inject import (
            append_cache_disclosure,
            build_cache_disclosure,
        )
        append_cache_disclosure(
            resp_headers,
            build_cache_disclosure(
                llm_hint=llm_hint,
                cache_decision=cache_decision,
                cache_injected=cache_injected,
                served_provider_type=route.provider.provider_type,
                usage=(anth_resp or {}).get("usage"),
            ),
        )
        return JSONResponse(
            content=anthropic_response_to_openai(anth_resp, requested_model=body.get("model") or ""),
            headers=resp_headers,
        )
