"""
/v1/messages — Anthropic-format endpoint (same path as v1).
Handles both streaming and non-streaming responses.
"""
import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Request, Depends, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.utils.disconnect_watchdog import watch_for_disconnect
from app.auth.keys import verify_api_key
from app.routing.router import select_provider
from app.routing.litellm_binding import clamp_thinking_budget
from app.cot.tool_emulation import (
    build_anthropic_tool_prompt,
    normalize_anthropic_messages,
    parse_tool_calls,
    call_with_tool_prompt,
    strip_thinking,
)
from app.cot.sse import (
    anthropic_tool_sse,
    anthropic_tools_sse,
    anthropic_text_sse,
    anthropic_tool_response,
    anthropic_tools_response,
    anthropic_text_response,
    to_anthropic_response,
)
from app.monitoring.helpers import record_outcome
from app.api.image_utils import has_images_anthropic, strip_images_anthropic
from app.routing.aliases import resolve_alias
from app.api._messages_streaming import (
    _stream_cot_anthropic, _stream_anthropic, _webhook_completion_anthropic,
    preflight_sse, http_status_for_stream_error,
    buffer_sse_until_content, stream_with_empty_guard,
)
from app.api._messages_dispatch import (
    dispatch_claude_oauth_chain, _select_excluding,
    try_cascade_dispatch,
)
from app.routing.retry import acompletion_with_retry
from app.observability.otel import llm_span
from app.cache.middleware import maybe_store
from app.routing.hedging import (
    should_hedge_header, wait_budget_ms, race_streams, try_acquire_hedge,
)
from app.config import settings
from app.observability.prometheus import (
    observe_hedge_attempt, observe_hedge_win, observe_hedge_bucket_reject,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# pool-leak-audit: watchdog+bounded
# The watch_for_disconnect dep cancels the handler on client abort;
# LLM streams are bounded by upstream provider timeouts (~60s max).
# See v5.7.17 and CHANGELOG v5.21.8.
@router.post("/v1/messages")
async def messages(
    request: Request,
    background_tasks: BackgroundTasks,
    # v5.7.17 — client-disconnect watchdog. Runs in parallel with the
    # handler; on disconnect cancels the handler task so ``async with
    # db: ...`` releases the DB connection. Closes the supervisor DB
    # pool leak (2026-06-16). Listed BEFORE db so the watchdog is set
    # up before any session is checked out.
    _watchdog: None = Depends(watch_for_disconnect),
    db: AsyncSession = Depends(get_db),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    llm_hint: Optional[str] = Header(None, alias="llm-hint"),
    x_session_id: Optional[str] = Header(None, alias="x-session-id"),
    x_cot_iterations: Optional[str] = Header(None, alias="x-cot-iterations"),
    x_cot_verify: Optional[str] = Header(None, alias="x-cot-verify"),
    x_cot_samples: Optional[str] = Header(None, alias="x-cot-samples"),
    x_cot_mode: Optional[str] = Header(None, alias="x-cot-mode"),
    x_webhook_url: Optional[str] = Header(None, alias="x-webhook-url"),
    anthropic_beta: Optional[str] = Header(None, alias="anthropic-beta"),
    x_cache: Optional[str] = Header(None, alias="x-cache"),
    x_cache_ttl: Optional[str] = Header(None, alias="x-cache-ttl"),
    x_hedge: Optional[str] = Header(None, alias="x-hedge"),
    x_cot_cascade: Optional[str] = Header(None, alias="x-cot-cascade"),
    x_context_strategy: Optional[str] = Header(None, alias="x-context-strategy"),
    x_conversation_id: Optional[str] = Header(None, alias="x-conversation-id"),
    x_memory_tag: Optional[str] = Header(None, alias="x-memory-tag"),
):
    # v5.7.18/v5.7.23 — pre-route setup extracted to
    # ``_handler_shared`` (was ``_messages_pre_route`` in v5.7.18;
    # Phase 2 lifted to shared with /v1/chat/completions). Behavior
    # unchanged; see the helper module for what fires here.
    from app.api._handler_shared import prepare_request_context
    key_record = await prepare_request_context(
        request, db, x_api_key,
        endpoint="messages",
        x_conversation_id=x_conversation_id,
        x_memory_tag=x_memory_tag,
    )

    body = await request.json()
    # v5.7.19/v5.7.23 — Phase 1 sub-block 2 was ``normalize_request_body``
    # in ``_messages_pre_route``; Phase 2 lifted it to ``_handler_shared``
    # so /v1/chat/completions uses the same logic. Behavior unchanged.
    from app.api._handler_shared import normalize_request_body
    body, _orig_request_model, parsed_slug, is_auto, alias = await normalize_request_body(
        body, x_webhook_url, db, endpoint="messages",
    )
    messages_list = body.get("messages", [])
    stream = body.get("stream", False)
    # v5.21.6 — buffered-cascade mode detection extracted to
    # ``_buffered_cascade_mode.detect_buffered_cascade_mode``. See that
    # module for the trade-off table (buffered vs buffered-heartbeat vs
    # pass-through).
    # v5.21.9 — this function no longer touches resp_headers directly
    # (it's not yet built at this point). Returns the header VALUE; we
    # stash it and apply after resp_headers exists (~line 476).
    from app.api._buffered_cascade_mode import detect_buffered_cascade_mode
    _buffered_cascade_stream, _buffered_cascade_heartbeat, _buffered_cascade_mode_hdr = detect_buffered_cascade_mode(
        stream, key_record,
    )
    if _buffered_cascade_stream:
        stream = False  # rest of the handler runs non-streaming
    max_tokens = body.get("max_tokens", 1024)
    system = body.get("system")
    thinking = body.get("thinking")
    # v5.6.0 / v5.7.1 / v5.6.1 — proxy-injected tools.
    #
    # v5.6.0: Anthropic-shape tool injection on /v1/messages (non-stream).
    # v5.7.1: switched to ``inject_anthropic_async`` which sources
    #   from BOTH the static registry (Excel) AND the FastMCP
    #   aggregator bridge (markitdown + future sub-server tools).
    # v5.6.1: lifted the ``if not stream`` gate. Streaming requests
    #   now get the same tool injection. Round-trip works via the
    #   client-side flow:
    #     1. Stream emits ``tool_use`` for the proxy tool.
    #     2. Client sends a follow-up /v1/messages with placeholder
    #        ``tool_result`` block.
    #     3. ``patch_inbound_tool_results`` (called BEFORE upstream
    #        dispatch) detects the tool_use → tool_result pair, runs
    #        the proxy tool server-side, and patches the placeholder
    #        with the real output. Upstream model never sees the
    #        placeholder.
    _proxy_tools_injected = False
    if True:  # was: if not stream — lifted in v5.6.1
        # v5.7.4 — propagate the API key's MCP policy into the
        # ContextVar the FastMCP wrapper consults so the bridge's
        # list_tools / call_tool round-trip filters by the same
        # allow/deny rules that the /mcp endpoint enforces. Without
        # this, Path B injection would expose tools the key is denied.
        _policy_token = None
        try:
            from app.mcp_server.server import current_mcp_policy
            _policy_token = current_mcp_policy.set({
                "mcp_tools_allow": getattr(key_record, "mcp_tools_allow", None),
                "mcp_tools_deny": getattr(key_record, "mcp_tools_deny", None),
                "mcp_schema_token_budget": getattr(
                    key_record, "mcp_schema_token_budget", None,
                ),
            })
        except Exception:
            pass  # mcp module not available (graceful)
        try:
            from app.proxy_tools import inject_anthropic_async
            await inject_anthropic_async(body)
            _proxy_tools_injected = True
        except Exception as exc:
            logger.warning("proxy_tools.inject_failed err=%s", exc)
        # v5.6.1 — server-side tool_result patcher for streaming
        # round-trips. When a streaming client receives a tool_use it
        # can't execute, it sends a follow-up /v1/messages with a
        # placeholder tool_result. We detect the (tool_use →
        # tool_result) pair, execute the real tool, and replace the
        # placeholder content BEFORE the upstream model sees it.
        try:
            from app.proxy_tools import patch_inbound_tool_results
            _patched = await patch_inbound_tool_results(body.get("messages") or [])
            if _patched:
                logger.info("proxy_tools.tool_result_patched count=%d", _patched)
        except Exception as exc:
            logger.warning("proxy_tools.patch_inbound_failed err=%s", exc)
        # Note: we INTENTIONALLY do not reset _policy_token here.
        # The response interception path (find_proxy_tool_use_async +
        # run_tool) also goes through the FastMCP wrapper and needs
        # the policy visible. The contextvar dies with the request
        # naturally when the handler returns.

    # v5.7.1 — system-prompt augmentation. Per-key opt-in flag in
    # ``api_keys.system_prompt_mcp_augmentation``. When True AND
    # tools were injected, prepend a one-line nudge to ``body["system"]``
    # telling the model that proxy-injected tools exist and to prefer
    # them over saying "I can't read X". Default off — operator opts
    # in per key.
    if _proxy_tools_injected and getattr(key_record, "system_prompt_mcp_augmentation", False):
        try:
            _MCP_NUDGE = (
                "You have access to proxy-injected tools for reading "
                "Excel/Word/PDF/PowerPoint/HTML/EPUB documents, "
                "fetching URLs, and converting documents to markdown. "
                "When the user asks about content that would benefit "
                "from these tools, call them instead of saying \"I "
                "can't read X\" or \"I don't have access\"."
            )
            existing_system = body.get("system")
            if isinstance(existing_system, str):
                body["system"] = _MCP_NUDGE + "\n\n" + existing_system
            elif isinstance(existing_system, list):
                # Anthropic also accepts a list-of-text-blocks shape
                body["system"] = [{"type": "text", "text": _MCP_NUDGE}] + existing_system
            else:
                body["system"] = _MCP_NUDGE
            system = body.get("system")
        except Exception as exc:
            logger.warning("proxy_tools.system_nudge_failed err=%s", exc)

    # v5.20.0 — refusal_prompt_hardening. Per-key opt-in that appends
    # "if you can't fulfill this, reply with REFUSED: <reason>"
    # instructions to body["system"]. Makes silent task substitution
    # ("I can't write X but here's Y") machine-detectable so the
    # response-tail can log/retry deterministically. Independent of
    # the v5.7.1 MCP nudge; a key can enable both.
    if getattr(key_record, "refusal_prompt_hardening", False):
        try:
            from app.refusal_detection import REFUSAL_HARDENING_INSTRUCTION
            existing_system = body.get("system")
            if isinstance(existing_system, str):
                body["system"] = REFUSAL_HARDENING_INSTRUCTION + "\n\n" + existing_system
            elif isinstance(existing_system, list):
                body["system"] = (
                    [{"type": "text", "text": REFUSAL_HARDENING_INSTRUCTION}]
                    + existing_system
                )
            else:
                body["system"] = REFUSAL_HARDENING_INSTRUCTION
            system = body.get("system")
        except Exception as exc:
            logger.warning("refusal_detection.hardening_failed err=%s", exc)
    tools = body.get("tools")

    from app.api._request_pipeline import (
        apply_privacy_filters, build_hint_with_auto_task,
        apply_context_compression, build_base_response_headers,
    )

    messages_list, _pii_masked_count = apply_privacy_filters(messages_list, body)
    # v5.21.2 — inject the per-key default refuse-tolerance dim into the
    # LMRH-Hint header when the caller didn't already specify one.
    # Caller-passed value ALWAYS wins over the per-key default. Header
    # injection happens BEFORE build_hint_with_auto_task so the parser
    # sees a single unified string.
    # v5.21.9 — stash whether we injected, apply header after
    # resp_headers is built (~line 479). Prior code touched
    # resp_headers here → UnboundLocalError for any key with a
    # ``default_refuse_tolerance`` set (latent bug, no key had it in
    # prod yet — caught by v5.21.9's regression pin).
    _key_rt_default = getattr(key_record, "default_refuse_tolerance", None)
    _lmrh_dim_injected: str | None = None
    if _key_rt_default and (llm_hint or "").find("refuse-tolerance=") < 0:
        _rt_dim = f"refuse-tolerance={_key_rt_default}"
        llm_hint = f"{llm_hint};{_rt_dim}" if llm_hint else _rt_dim
        _lmrh_dim_injected = "refuse-tolerance"
    hint, auto_task = await build_hint_with_auto_task(llm_hint, messages_list)
    has_tools = bool(tools)
    has_images = has_images_anthropic(messages_list)

    # v3.9.5 (#267 Phase 8) — memory injection moved here from pre-route
    # to post-route so we can gate on route.provider.memory_disabled.
    # The actual inject call site is below, after Phase 6 flush and
    # cross-family body rewrite, but before Fix B translation (which
    # consumes Anthropic-shape body['system']).
    _mem_injected = False

    # v5.7.19 — suffix-strip + embedding guard + auto-resolution moved
    # into ``normalize_request_body`` above. parsed_slug, is_auto, alias
    # are unpacked at the call site.
    # v3.0.x refactor: provider selection + 503 conversion + auto-model
    # resolution moved into _request_pipeline shared helpers. Both
    # /v1/messages and /v1/chat/completions now go through the same code
    # path here — prevents future divergence bugs like the v3.0.99
    # gemini-routing-to-claude-oauth incident.
    from app.api._request_pipeline import (
        select_provider_with_503, resolve_auto_model_into_body,
    )
    # v5.0.0 → v5.0.9 — `ComplianceNoSubstituteError` / `…NoLocalProviderError`
    # → 503 conversion extracted to ``_compliance_handler.raise_for_no_substitute_exception``.
    # The except catches both because NoLocalProvider is a subclass.
    from app.compliance import ComplianceNoSubstituteError
    from app.api._compliance_handler import raise_for_no_substitute_exception
    try:
        route = await select_provider_with_503(
            db, hint,
            has_tools=has_tools, has_images=has_images,
            key_record=key_record, parsed_slug=parsed_slug, alias=alias,
            detailed_503=True,
            messages=body.get("messages"),
        )
    except ComplianceNoSubstituteError as _exc:
        await raise_for_no_substitute_exception(
            _exc, request=request, db=db, key_record=key_record,
            orig_request_model=_orig_request_model,
        )

    # v3.9.1 (#269 Fix A) — Safety net for cross-family fallback to
    # non-OpenAI-shape providers (e.g. Gemini, Cohere) when the request
    # carries Anthropic-shape tool_use/tool_result content. The B
    # translator below covers OpenAI-shape targets; everything else
    # would still 400 on the upstream, so we walk past those providers
    # until either an OpenAI-shape provider is picked (Fix B handles it)
    # or every cross-family path is exhausted — 503 cleanly in the
    # latter case rather than burning upstream cost on guaranteed-400.
    from app.routing.tool_content import (
        has_anthropic_tool_content, has_anthropic_tool_defs,
    )
    _openai_shape_providers = {
        "openai", "openrouter", "grok", "grok-bridge", "grok-web",
        "groq", "mistral", "perplexity", "ollama", "deepseek", "fireworks",
    }
    _anthropic_types = {"anthropic", "claude-oauth"}
    _has_tool_blocks = has_anthropic_tool_content(messages_list)
    _cross_family_skipped: list[str] = []
    if _has_tool_blocks:
        while (
            route.cross_family_fallback
            and route.profile.provider_type not in _anthropic_types
            and route.profile.provider_type not in _openai_shape_providers
        ):
            _cross_family_skipped.append(route.provider.name)
            tried_ids = {route.provider.id, *(
                getattr(route, "_tried_ids", []) or []
            )}
            try:
                route = await _select_excluding(
                    db, hint, has_tools, has_images, key_record.key_type,
                    tried_ids, api_key_id=key_record.id,
                )
            except Exception:
                raise HTTPException(
                    503,
                    "Cross-family fallback to a non-translatable upstream "
                    "for a tool-using Anthropic request. Providers skipped: "
                    f"{', '.join(_cross_family_skipped)}",
                )

    body = resolve_auto_model_into_body(body, route, is_auto)

    # v3.0.36: cross-family fallback — rewrite body['model'] to the resolved
    # served model for the claude-oauth dispatcher (which reads body['model']
    # not route.litellm_model). Original requested model surfaced in
    # LLM-Capability response header.
    if route.cross_family_fallback and route.served_model_native:
        body = {**body, "model": route.served_model_native}

    # v3.9.3 (#267) Phase 6 — proxy-side caller-memory provider flush.
    # All routing decisions (incl. cross-family fallback + safety-net
    # walking) are now final. If the memory marker shows a different
    # provider was the last writer for this (api_key, conversation,
    # tag), emit a best-effort flush to that old provider and bump the
    # marker. Silent degrade on failure — never breaks live traffic.
    from app.memory.flush import maybe_flush_provider_memory
    await maybe_flush_provider_memory(
        db, api_key_id=key_record.id,
        conversation_id=x_conversation_id, memory_tag=x_memory_tag,
        new_provider_id=route.provider.id,
    )

    # v3.8.9 (#267) Phase 4 — proxy-side caller-memory injection.
    # Relocated here in v3.9.5 (Phase 8) so we can gate on the chosen
    # provider's memory_disabled flag. Must run BEFORE Fix B translation
    # below (which consumes Anthropic-shape body['system']).
    # Silent degrade on any store error.
    if not getattr(route.provider, "memory_disabled", False):
        from app.memory.inject import maybe_inject_memory
        body, _mem_injected = await maybe_inject_memory(
            db, body=body, api_key_id=key_record.id,
            conversation_id=x_conversation_id, memory_tag=x_memory_tag,
            endpoint="messages",
        )
        if _mem_injected:
            # Re-extract since we may have just prepended to system / messages.
            system = body.get("system")
            messages_list = body.get("messages", messages_list)

    # v5.7.20 — Phase 1 sub-block 3 of the messages.py extract: the
    # Anthropic→OpenAI body translation block moved into
    # ``_messages_pre_route.translate_to_openai_if_needed``. Behavior
    # unchanged; the helper returns the (possibly translated) body
    # + system + messages_list + tools + ``translated`` flag.
    from app.api._messages_pre_route import translate_to_openai_if_needed
    body, system, messages_list, tools, _cross_family_translated = translate_to_openai_if_needed(
        body=body,
        route=route,
        system=system,
        messages_list=messages_list,
        tools=tools,
        has_tool_blocks=_has_tool_blocks,
        has_images=has_images,
    )

    # OTEL GenAI span: routing-decision metadata (no-op if OTLP endpoint unset)
    with llm_span(
        operation="chat",
        provider_type=route.profile.provider_type,
        requested_model=body.get("model") or "",
        resolved_model=route.litellm_model,
        lmrh_hint=llm_hint,
        cot_engaged=route.cot_engaged,
        unmet_hints=route.unmet_hints,
        extra={"gen_ai.request.max_tokens": max_tokens},
    ):
        pass

    # Build extra kwargs for litellm
    extra = {**route.litellm_kwargs, "max_tokens": max_tokens}
    if system:
        extra["system"] = system
    if tools:
        extra["tools"] = tools
    # Native reasoning injection:
    # - Gemini 2.5 / o-series: inject from router-computed params
    # - Anthropic extended-thinking: forward the client's `thinking` block as-is
    if route.native_thinking_params:
        extra.update(route.native_thinking_params)
        # v5.3.7 — keep Gemini thinking budget below max_tokens (empty-success fix)
        clamp_thinking_budget(extra)
    elif thinking and route.profile.provider_type == "anthropic":
        extra["thinking"] = thinking

    # Forward anthropic-beta header when routing to Anthropic — some cache
    # directives (e.g. 1-hour TTL) require this. No-op for other providers.
    if anthropic_beta and route.profile.provider_type == "anthropic":
        extra["extra_headers"] = {"anthropic-beta": anthropic_beta}

    # v5.21.3 — heartbeat mode early-return. When both
    # ``refusal_retry_enabled`` AND
    # ``refusal_retry_streaming_heartbeat`` are on for this key AND the
    # caller asked for stream=true, delegate to the buffered-cascade
    # streaming helper which returns a StreamingResponse whose
    # generator emits SSE keepalive frames DURING the dispatch. Skips
    # the rest of the handler (tool hops, memory injection, MCP
    # injection, response tail) — that's the documented trade-off.
    # v5.21.0 no-heartbeat mode falls through unchanged.
    # v5.21.9 — early-return moved BELOW resp_headers construction (was
    # here in v5.21.3, referenced resp_headers before it existed →
    # UnboundLocalError for any key with refusal_retry_streaming_heartbeat=True).
    # Real early-return is at "buffered-heartbeat early-return v5.21.9"
    # marker below.

    vision_routed_count = 0
    if route.vision_stripped:
        if settings.vision_route_enabled:
            from app.api.vision_route import transcribe_anthropic
            messages_list, vision_routed_count = await transcribe_anthropic(
                messages_list, db, exclude_provider_id=route.provider.id,
            )
        else:
            messages_list = strip_images_anthropic(messages_list)

    messages_list, context_strategy_applied = await apply_context_compression(
        messages_list,
        route=route,
        x_context_strategy=x_context_strategy,
        extra=extra,
        system=str(system or ""),
    )

    resp_headers = build_base_response_headers(
        route=route,
        auto_task=auto_task,
        vision_routed_count=vision_routed_count,
        context_strategy_applied=context_strategy_applied,
        pii_masked_count=_pii_masked_count,
        hint=hint,
        max_tokens=max_tokens,
    )
    # v5.21.9 — apply the buffered-cascade mode header stashed at
    # request entry (see line 120 area). Was previously mutated
    # directly into resp_headers by detect_buffered_cascade_mode,
    # which required resp_headers to already exist. Bug: it didn't.
    if _buffered_cascade_mode_hdr:
        resp_headers["X-Refusal-Cascade-Mode"] = _buffered_cascade_mode_hdr
    # v5.21.9 — same class as above: v5.21.2 injected the LMRH dim
    # header directly into resp_headers before it existed.
    if _lmrh_dim_injected:
        resp_headers["X-LMRH-Injected-Dim"] = _lmrh_dim_injected

    # buffered-heartbeat early-return v5.21.9 — moved here (from ~line 447)
    # so resp_headers is populated when the StreamingResponse is built.
    if _buffered_cascade_heartbeat:
        from app.api._buffered_cascade_stream import (
            run_buffered_cascade_stream_with_heartbeat,
        )
        return StreamingResponse(
            run_buffered_cascade_stream_with_heartbeat(
                route=route,
                key_record=key_record,
                messages_list=messages_list,
                extra=extra,
                system=system,
                max_tokens=max_tokens,
                has_images=has_images,
                hint=hint,
                db=db,
            ),
            media_type="text/event-stream",
            headers=resp_headers,
        )
    # v2.8.0 — surface the slug-shortcut + auto-routing decision so clients
    # can introspect what happened (parity with OpenRouter's response.model).
    if parsed_slug.sort_mode:
        resp_headers["X-Sort-Mode"] = parsed_slug.sort_mode
    if is_auto:
        resp_headers["X-Auto-Routed"] = f"{route.provider.name}:{route.profile.model_id}"
    if _mem_injected:
        resp_headers["X-Caller-Memory"] = "injected"
    if _cross_family_skipped:
        resp_headers["X-Cross-Family-Skipped"] = ",".join(_cross_family_skipped)
    if _cross_family_translated:
        resp_headers["X-Cross-Family-Translated"] = "anthropic->openai"
    # Budget visibility headers (soft-cap warning, remaining $ today/this hour)
    if key_record.budget_status is not None:
        from app.budget.tracker import warnings_for
        resp_headers.update(warnings_for(key_record.budget_status))

    # v5.0.0 — compliance substitution disclosure (decision 8 + 15 + 23).
    # When the router pre-filter forced a substitution, surface the 7
    # X-Compliance-* headers + audit row, and prepare the SSE-prelude
    # payload for the streaming paths below.
    # v5.0.9 — substitution disclosure extracted to
    # ``_compliance_handler.emit_substitution_disclosure_for_route``.
    from app.api._compliance_handler import emit_substitution_disclosure_for_route
    _headers_to_merge, _compliance_disclosure, _compliance_wants_sse_prelude = (
        await emit_substitution_disclosure_for_route(
            request, db, route, key_record, _orig_request_model,
        )
    )
    if _headers_to_merge:
        resp_headers.update(_headers_to_merge)

    # ── claude-oauth dispatch ──────────────────────────────────────────────
    # The claude-oauth provider-chain walk lives in _messages_dispatch.py
    # (extracted v3.10.9). It short-circuits the pipeline when the route is
    # claude-oauth and returns a Response; otherwise it returns (None, route)
    # with route advanced to a non-claude-oauth provider so we fall through
    # to the litellm path below.
    _route_pre_dispatch = route
    _oauth_response, route = await dispatch_claude_oauth_chain(
        route,
        body=body, db=db, key_record=key_record, resp_headers=resp_headers,
        stream=stream, max_tokens=max_tokens, llm_hint=llm_hint,
        hint=hint, has_tools=has_tools, has_images=has_images,
        conversation_id=x_conversation_id, memory_tag=x_memory_tag,
    )
    if _oauth_response is not None:
        return _oauth_response
    # v3.10.12 BUG-024: dispatch_claude_oauth_chain can advance `route`
    # to a litellm provider after exhausting the claude-oauth chain.
    # `extra` was built (above) from the pre-fallthrough route's
    # litellm_kwargs — swap in the NEW route's so the litellm dispatch
    # below uses the right credentials / base_url / headers, not the
    # dead OAuth provider's.
    if route is not _route_pre_dispatch:
        for _k in _route_pre_dispatch.litellm_kwargs:
            extra.pop(_k, None)
        extra.update(route.litellm_kwargs)

    # ── v3.2.0: grok-web dispatch ──────────────────────────────────────────
    # Operator's grok.com web subscription. Like claude-oauth/codex-oauth,
    # short-circuits the rest of the pipeline (no CoT, no tool emulation,
    # no cascade) — Grok serves a single text response and we return it.
    # v3.2.9: shared dispatcher in _grok_web_dispatch (was duplicated 1:1
    # in completions.py). See design.md "Subscription-as-a-provider pattern".
    # v5.0.23 / remediation Batch 2.5 — failover wiring: on a
    # failover-eligible failure (any GrokWebError that maps to 502 or
    # 429), the dispatcher returns None instead of raising. We
    # re-select a provider that excludes the failed grok-web id (the
    # router naturally picks OpenRouter for grok-3 next in priority)
    # and fall through to the litellm dispatch path below.
    if route.provider.provider_type == "grok-web":
        from app.api._grok_web_dispatch import dispatch_grok_web_anthropic
        gw_resp = await dispatch_grok_web_anthropic(
            route=route, body=body, stream=stream, resp_headers=resp_headers,
            db=db, key_record_id=key_record.id, t0=time.monotonic(),
            llm_hint=llm_hint,
        )
        if gw_resp is not None:
            return gw_resp
        # Failover — re-resolve route excluding the failed grok-web
        # provider. select_provider returns the next-priority match
        # (OpenRouter for grok-3 in the standard fleet config). If
        # nothing else can serve the request, propagate a 502 so the
        # caller knows the chain is exhausted.
        from app.routing.router import select_provider
        failed_id = route.provider.id
        new_route = await select_provider(
            db=db, hint=hint,
            has_tools=_has_tool_blocks, has_images=False,
            key_type=key_record.key_type or "standard",
            model_override=body.get("model"),
            exclude_provider_id=failed_id,
        )
        if new_route is None or new_route.provider.provider_type == "grok-web":
            raise HTTPException(
                502,
                "grok-web upstream failed and no alternative provider "
                "is available for the requested model.",
            )
        logger.info(
            "grok_web.failover_to provider=%s (was=%s, model=%s)",
            new_route.provider.name, failed_id, body.get("model"),
        )
        # v5.0.23 — preserve the caller's model intent through the
        # failover (see completions.py for the symmetric rationale).
        new_route.cross_family_fallback = False
        new_route.served_model_native = None
        from app.routing.litellm_binding import build_litellm_model as _bld
        new_route.litellm_model = _bld(new_route.provider, body.get("model"))
        # v5.1.0 / Batch A4 — swap extra (litellm_kwargs) to the new
        # provider's. See completions.py for the rationale.
        for _k in list(route.litellm_kwargs.keys()):
            extra.pop(_k, None)
        extra.update(new_route.litellm_kwargs)
        route = new_route
        resp_headers["X-Grok-Web-Failover"] = "true"
        resp_headers["X-Grok-Web-Failover-Target"] = new_route.provider.provider_type
        # Fall through to the litellm dispatch path below with the
        # new route in scope.

    # Semantic cache — check before anything LLM-ish runs.
    # v3.5.x R1 (2026-05-09): orchestration extracted to
    # _request_pipeline.maybe_serve_from_cache so the same logic isn't
    # also copy-pasted into completions.py. Wire-format builders
    # (anthropic_text_sse / anthropic_text_response) are passed in.
    from app.api._request_pipeline import maybe_serve_from_cache
    cache_decision, cache_resp = await maybe_serve_from_cache(
        x_cache_header=x_cache,
        api_key_opt_in=bool(getattr(key_record, "semantic_cache_enabled", False)),
        key_type=key_record.key_type,
        route=route,
        has_tools=has_tools,
        webhook_url=x_webhook_url,
        body=body,
        messages_list=messages_list,
        system=system,
        tools=tools,
        x_cache_ttl_header=x_cache_ttl,
        tenant_id=key_record.id,
        endpoint="messages",
        text_sse_fn=anthropic_text_sse,
        text_response_fn=anthropic_text_response,
        resp_headers=resp_headers,
        stream=stream,
    )
    if cache_resp is not None:
        return cache_resp

    # Webhook async: fire-and-forget completion, return 202 immediately
    if x_webhook_url:
        background_tasks.add_task(
            _webhook_completion_anthropic,
            x_webhook_url, route.litellm_model, messages_list, extra,
            route.provider.id, db, key_record.id,
            llm_hint=llm_hint,
        )
        return JSONResponse(
            {"status": "queued", "webhook_url": x_webhook_url},
            status_code=202,
            headers=resp_headers,
        )

    try:
        if route.tool_emulation_engaged:
            # Wave 5 #23 — respect parallel_tool_calls from the inbound body
            # (Anthropic expresses this as tool_choice={disable_parallel_tool_use:true})
            tool_choice = body.get("tool_choice") or {}
            allow_parallel = True
            if isinstance(tool_choice, dict) and tool_choice.get("disable_parallel_tool_use"):
                allow_parallel = False
            # v4.1.1 — co-emulation: when CoT-E is also engaged on this request
            # the tool prompt is reasoning-prefixed, so the model thinks step
            # by step before emitting tool calls (tools + reasoning together).
            tool_system = build_anthropic_tool_prompt(
                tools or [], allow_parallel=allow_parallel,
                with_reasoning=route.cot_engaged,
            )
            merged_system = tool_system + ("\n\n" + system if system else "")
            norm_msgs = normalize_anthropic_messages(messages_list)
            emul_extra = {k: v for k, v in extra.items() if k not in ("tools", "system")}
            response_text = await call_with_tool_prompt(
                route.litellm_model, norm_msgs, merged_system, emul_extra
            )
            tool_calls = parse_tool_calls(response_text)
            if route.cot_engaged:
                # drop the <thinking> block from the plain-text fallback
                response_text = strip_thinking(response_text)
            # v3.8.3 (#263) — emit telemetry BEFORE response building so
            # the activity_log row carries the count + validation flag
            # for this emulated tool-call request. Note: response_body
            # is constructed below from tool_calls; we re-derive a
            # synthetic Anthropic-shape response for the meta extractor
            # so success-rate aggregation across native + emulated paths
            # uses the same response_body schema.
            _emul_resp_body = {
                "content": [
                    {"type": "tool_use", "name": tc.get("name", ""),
                     "input": tc.get("input", {}) if isinstance(tc.get("input"), dict) else {}}
                    for tc in tool_calls
                ],
            } if tool_calls else {"content": []}
            await record_outcome(
                db, route.provider.id, route.litellm_model, success=True,
                t0=time.monotonic(), key_record_id=key_record.id,
                response_body=_emul_resp_body,
                tool_call_format="emulated",
            )
            # Enforce serial when parallel is disabled
            if not allow_parallel and len(tool_calls) > 1:
                tool_calls = tool_calls[:1]
            if tool_calls:
                resp_headers["X-Tool-Calls-Emitted"] = str(len(tool_calls))
            if stream:
                if len(tool_calls) >= 2:
                    gen = anthropic_tools_sse(tool_calls)
                elif len(tool_calls) == 1:
                    gen = anthropic_tool_sse(tool_calls[0]["name"], tool_calls[0]["input"])
                else:
                    gen = anthropic_text_sse(response_text)
                return StreamingResponse(gen, media_type="text/event-stream", headers=resp_headers)
            else:
                if len(tool_calls) >= 2:
                    content = anthropic_tools_response(tool_calls, route.litellm_model)
                elif len(tool_calls) == 1:
                    content = anthropic_tool_response(tool_calls[0]["name"], tool_calls[0]["input"], route.litellm_model)
                else:
                    content = anthropic_text_response(response_text, route.litellm_model)
                # v3.6.1 — merge X-Quality-Hint for tool-emulation path
                from app.api._quality_hint import merge_into_headers
                merge_into_headers(resp_headers, content, endpoint="messages")
                return JSONResponse(content=content, headers=resp_headers)

        # CoT-E engagement.
        # v3.5.x R2 (2026-05-09): orchestration extracted to
        # _request_pipeline.maybe_engage_cot. The Anthropic flow passes
        # ``requested_model`` + ``llm_hint`` through extra_kwargs_for_stream
        # because _stream_cot_anthropic accepts them; the OpenAI flow
        # doesn't. The helper handles header parsing, task-branch
        # selection, cross-provider critique pick, and StreamingResponse
        # construction (all identical between the two endpoints).
        from app.api._request_pipeline import maybe_engage_cot
        cot_resp = await maybe_engage_cot(
            route=route, stream=stream, db=db, key_record=key_record,
            hint=hint, body=body, messages_list=messages_list, extra=extra,
            x_cot_iterations=x_cot_iterations, x_cot_verify=x_cot_verify,
            x_cot_samples=x_cot_samples, x_cot_mode=x_cot_mode,
            x_session_id=x_session_id,
            resp_headers=resp_headers,
            stream_cot_fn=_stream_cot_anthropic,
            extra_kwargs_for_stream={
                "requested_model": body.get("model") if isinstance(body, dict) else "",
                "llm_hint": llm_hint,
                # v3.10.11 — thread caller-memory context into the CoT
                # streaming path so it runs memory write-back too.
                "conversation_id": x_conversation_id,
                "memory_tag": x_memory_tag,
            },
        )
        if cot_resp is not None:
            return cot_resp

        if stream:
            # Hedging: if opted in and we have a TTFT p95 signal for the primary
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
                        if tools: b_extra["tools"] = tools
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
                    if tools: _e["tools"] = tools
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
        else:
            t0 = time.monotonic()
            # Wave 3 #17 — ordered fallback across ranked providers
            from app.routing.fallback import try_ranked_non_streaming
            # Wave 3 #14 — cascade routing (cheap → grade → escalate)
            from app.routing.cascade import cascade_requested, grade_answer
            lmrh_cascade = hint.get("cascade").value if (hint and hint.get("cascade")) else None
            do_cascade = cascade_requested(lmrh_cascade, x_cot_cascade)

            async def _call_with_route(r):
                # Rebuild extra kwargs for THIS route's provider (api_key, etc.)
                local_extra = {**r.litellm_kwargs, "max_tokens": max_tokens}
                if system:
                    local_extra["system"] = system
                if tools:
                    local_extra["tools"] = tools
                if r.native_thinking_params:
                    local_extra.update(r.native_thinking_params)
                    # v5.3.7 — keep Gemini thinking budget below max_tokens (empty-success fix)
                    clamp_thinking_budget(local_extra)
                elif thinking and r.profile.provider_type == "anthropic":
                    local_extra["thinking"] = thinking
                if anthropic_beta and r.profile.provider_type == "anthropic":
                    local_extra["extra_headers"] = {"anthropic-beta": anthropic_beta}
                # v5.15.1 (#508 Phase 2) — per-account OAuth fan-out. Swap
                # ``local_extra['api_key']`` to the picked account's token
                # when the provider is OAuth-flavored (cursor-oauth today,
                # codex-oauth + claude-oauth same code path once operator
                # seeds accounts). No-op for non-OAuth providers.
                from app.providers.oauth_account_selector import apply_fanout_to_kwargs
                _oauth_account_id = await apply_fanout_to_kwargs(
                    local_extra, r.provider, db,
                )
                if _oauth_account_id:
                    resp_headers["X-OAuth-Account"] = _oauth_account_id
                return await acompletion_with_retry(
                    model=r.litellm_model, messages=messages_list,
                    stream=False, **local_extra,
                )

            # Cascade: cheap first, grade, escalate only on reject.
            # v4.4.38: cascade orchestration extracted to
            # ``_messages_dispatch.try_cascade_dispatch`` — returns a
            # JSONResponse (accept) or None (escalate / error → fall
            # through to the regular non-streaming path below). resp_headers
            # are mutated in place to reflect X-Cascade-* attribution.
            if do_cascade and not has_tools and not route.cot_engaged:
                cascade_resp = await try_cascade_dispatch(
                    route,
                    db=db, key_record=key_record, hint=hint,
                    has_images=has_images, messages_list=messages_list,
                    cache_decision=cache_decision, resp_headers=resp_headers,
                    max_tokens=max_tokens, t0=t0,
                    call_with_route=_call_with_route,
                )
                if cascade_resp is not None:
                    return cascade_resp

            if settings.fallback_enabled:
                result, final_route, chain = await try_ranked_non_streaming(
                    db, hint,
                    has_tools=has_tools, has_images=has_images,
                    key_type=key_record.key_type,
                    pinned_provider_id=alias.provider_id if alias else None,
                    model_override=alias.model_id if alias else None,
                    primary_route=route, call_fn=_call_with_route,
                )
                if len(chain.attempts) > 1:
                    resp_headers["X-Fallback-Chain"] = chain.as_header()
                    resp_headers["X-Provider"] = final_route.provider.name
                    resp_headers["X-Resolved-Model"] = final_route.litellm_model
                    route = final_route  # for record_outcome below
            else:
                result = await acompletion_with_retry(
                    model=route.litellm_model,
                    messages=messages_list,
                    stream=False,
                    **extra,
                )
            in_tok = getattr(result.usage, "prompt_tokens", 0)
            out_tok = getattr(result.usage, "completion_tokens", 0)
            from app.cot.sse import extract_cache_tokens
            cache_creation, cache_read = extract_cache_tokens(result.usage)
            await record_outcome(
                db, route.provider.id, route.litellm_model, success=True,
                in_tok=in_tok, out_tok=out_tok, t0=t0,
                key_record_id=key_record.id,
                cache_creation=cache_creation, cache_read=cache_read,
                provider_name=route.provider.name,
                # v3.0.35: body capture + diagnostic fields. Anthropic-shape
                # response is converted via to_anthropic_response for activity
                # log so the captured shape matches what the client received.
                request_body=body,
                response_body=to_anthropic_response(result),
                requested_model=body.get("model") if isinstance(body, dict) else None,
                had_lmrh_hint=bool(llm_hint),
                lmrh_hint_raw=llm_hint or None,
                # v3.8.3 (#263) — stamp tool_call_format when the
                # request carried tools=[]. The to_anthropic_response
                # body is what the meta extractor walks.
                tool_call_format=("native" if has_tools else None),
            )
            # Store in semantic cache (fire-and-forget; won't affect response latency)
            try:
                answer_text = result.choices[0].message.content or ""
                await maybe_store(cache_decision, answer_text)
            except Exception:
                pass

            # Wave 3 #16 — shadow traffic (sampled, fire-and-forget)
            if (settings.shadow_traffic_rate > 0
                and settings.shadow_candidate_provider_id
                and settings.shadow_candidate_provider_id != route.provider.id
                and not has_tools):
                from app.routing.shadow import should_shadow, run_shadow_compare
                if should_shadow(settings.shadow_traffic_rate):
                    from app.models.database import AsyncSessionLocal
                    background_tasks.add_task(
                        run_shadow_compare,
                        AsyncSessionLocal,
                        settings.shadow_candidate_provider_id,
                        messages_list,
                        answer_text,
                        route.litellm_model,
                        {"max_tokens": max_tokens,
                         **({"system": system} if system else {})},
                        settings.semantic_cache_embedding_model,
                        settings.semantic_cache_embedding_dims,
                        key_record.id,
                    )
                    resp_headers["X-Shadow-Queued"] = settings.shadow_candidate_provider_id

            remaining = max(0, max_tokens - out_tok)
            resp_headers["X-Token-Budget-Remaining"] = str(remaining)
            anthropic_result = to_anthropic_response(result)
            # v5.6.0 / v5.7.1 — proxy-injected-tool interception. If
            # the model invoked one of our injected tools (Excel,
            # markitdown, any MCP-aggregator tool), run it in-process
            # and re-call the model with the tool_result so the
            # assistant turn the CALLER sees has the file content
            # already incorporated. Capped at 3 hops to prevent a
            # runaway loop if the model keeps calling the tool.
            #
            # v5.7.1: switched to ``find_proxy_tool_use_async`` so
            # both static-registry tools AND MCP-aggregator-bridge
            # tools are recognized.
            if _proxy_tools_injected:
                from app.proxy_tools import (
                    find_proxy_tool_use_async, run_tool, build_tool_result_message,
                )
                _proxy_hops = 0
                while _proxy_hops < 3:
                    match = await find_proxy_tool_use_async(
                        anthropic_result.get("content") or []
                    )
                    if not match:
                        break
                    _proxy_hops += 1
                    proxy_tool, input_obj, tool_use_id = match
                    tool_output = await run_tool(proxy_tool, input_obj)
                    messages_list = list(messages_list) + [
                        {
                            "role": "assistant",
                            "content": anthropic_result.get("content") or [],
                        },
                        build_tool_result_message(tool_use_id, tool_output),
                    ]
                    result = await acompletion_with_retry(
                        model=route.litellm_model,
                        messages=messages_list,
                        stream=False,
                        **extra,
                    )
                    anthropic_result = to_anthropic_response(result)
                resp_headers["X-Proxy-Tool-Hops"] = str(_proxy_hops)
            # v3.9.0 (#267) Phase 5 — memory-tool write-back on litellm path.
            from app.memory.extract import maybe_extract_memory_writes
            mem_writes = await maybe_extract_memory_writes(
                db, response_dict=anthropic_result,
                api_key_id=key_record.id,
                conversation_id=x_conversation_id,
                memory_tag_default=x_memory_tag,
                source_provider_id=route.provider.id,
            )
            if mem_writes:
                resp_headers["X-Caller-Memory-Writes"] = str(mem_writes)
            # v5.20.1 — refusal cascade. When ``refusal_retry_enabled``
            # is on for this key AND detection fires on the initial
            # response, walk alternate providers (excluding those
            # already tried) until one produces a clean response or
            # max_attempts is exhausted. Non-streaming path only in
            # v5.20.1; streaming cascade is v5.20.2+. Emits
            # X-Refusal-Retry-* response headers + activity_log rows
            # for every attempt so the operator has full attribution.
            try:
                from app.api._refusal_cascade import maybe_cascade_on_refusal

                async def _cascade_dispatch(alt_route):
                    # Rebuild the litellm dispatch for a different route.
                    # Uses ``acompletion_with_retry`` — same primitive
                    # the initial call used. Doesn't re-run privacy /
                    # budget filters (those already ran on the initial
                    # dispatch). Uses the same messages_list so the model
                    # sees the same request.
                    _extra = dict(extra)
                    if system:
                        _extra["system"] = system
                    return await acompletion_with_retry(
                        model=alt_route.litellm_model,
                        messages=messages_list,
                        stream=False,
                        **_extra,
                    )

                _cascade = await maybe_cascade_on_refusal(
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
                    to_anthropic_response=to_anthropic_response,
                    resp_headers=resp_headers,
                    body=body,
                )
                if _cascade.swapped:
                    route = _cascade.final_route
                    result = _cascade.final_result
                    anthropic_result = _cascade.final_anthropic
            except Exception as exc:
                logger.warning("refusal_cascade.wrapper_failed err=%s", exc)

            # v5.7.6 — capability scout. Off by default; flips on via
            # the capability_scout.enabled system_setting. Fire-and-
            # forget; never blocks the response.
            try:
                from app.capability_scout.scout import scan_and_emit_for_response
                _n = await scan_and_emit_for_response(
                    db=db,
                    api_key_id=key_record.id,
                    provider_id=route.provider.id,
                    anthropic_response=anthropic_result,
                )
                if _n:
                    resp_headers["X-Capability-Scout-Suggestions"] = str(_n)
            except Exception:
                pass
            # v5.10.0 Ship 1 — emit X-Proxy-MCP-Suggestion when this
            # caller's accumulated score crosses the threshold. Score
            # was just bumped above by scan_and_emit_for_response when
            # a refusal pattern hit; this read sees the fresh value.
            # v5.19.0 — response-tail extracted to _messages_response_tail.
            # Runs the four post-dispatch header/hook blocks:
            # (1) capability-scout suggestion header
            # (2) accept-MCP handler (X-Proxy-Accept-MCP + x-llmproxy-config blob)
            # (3) x-llmproxy-config echo
            # (4) response hooks runner (substitution header + outbound callback)
            # Preserves per-block Exception-swallow posture so downstream
            # ships that add more tail blocks don't need to re-derive the
            # wiring in-place.
            from app.api._messages_response_tail import apply_response_tail
            await apply_response_tail(
                request=request,
                route=route,
                key_record=key_record,
                resp_headers=resp_headers,
                body=body,
                db=db,
                anthropic_result=anthropic_result,
            )
            # v5.20.11 — buffered-cascade streaming: convert the final
            # ``anthropic_result`` to SSE frames and return a
            # StreamingResponse. Text-only path uses ``anthropic_text_sse``
            # (extracts concatenated text blocks); if the result contains
            # tool_use blocks, fall back to a synthesized SSE stream that
            # emits each block. Everything else — image blocks etc — is
            # dropped to text-summary since streaming those in Anthropic's
            # SSE format is out of scope for the cascade shim.
            if _buffered_cascade_stream:
                try:
                    # v5.21.1 bugfix — the redundant local `from app.cot.sse
                    # import anthropic_text_sse, ...` here made those names
                    # LOCAL to the whole `messages()` function. Line 592 then
                    # accessed them BEFORE this line ran → UnboundLocalError
                    # on every non-buffered-cascade request. Names are
                    # already imported at module level (lines 27-29).
                    content_blocks = anthropic_result.get("content") or []
                    tool_uses = [b for b in content_blocks if b.get("type") == "tool_use"]
                    if len(tool_uses) >= 2:
                        gen = anthropic_tools_sse([
                            {"name": t["name"], "input": t.get("input", {})}
                            for t in tool_uses
                        ])
                    elif len(tool_uses) == 1:
                        gen = anthropic_tool_sse(
                            tool_uses[0]["name"], tool_uses[0].get("input", {}),
                        )
                    else:
                        text = "".join(
                            b.get("text", "") for b in content_blocks
                            if b.get("type") == "text"
                        )
                        gen = anthropic_text_sse(text)
                    return StreamingResponse(
                        gen, media_type="text/event-stream",
                        headers=resp_headers,
                    )
                except Exception:
                    # Fall through to JSON response if SSE conversion breaks.
                    # Caller's SSE parser will fail; header
                    # X-Refusal-Cascade-Mode still marks this as buffered.
                    pass
            return JSONResponse(
                content=anthropic_result,
                headers=resp_headers,
            )

    except Exception as e:
        err_str = str(e)
        await record_outcome(
            db, route.provider.id, route.litellm_model, success=False,
            key_record_id=key_record.id, error_str=err_str,
            provider_name=route.provider.name,
            request_body=body,
            requested_model=body.get("model") if isinstance(body, dict) else None,
            had_lmrh_hint=bool(llm_hint),
            lmrh_hint_raw=llm_hint or None,
        )
        logger.error(f"Provider {route.provider.id} failed: {err_str}")
        # v3.5.8 BUG-007/008 fix — sanitize before sending to client.
        # Pre-fix the raw litellm/Gemini exception text leaked
        # /usr/local/lib/python3.13/site-packages/litellm/... paths.
        from app.api._input_validation import sanitize_upstream_error
        from app.routing.circuit_breaker import classify_error
        cls = classify_error(err_str or "")
        clean = sanitize_upstream_error(err_str)
        # bad_request → HTTP 400 (it was the caller's fault); other
        # classes stay as 502 (upstream / network / billing).
        status_code = 400 if cls == "bad_request" else 502
        # v5.0.0 — when the dispatch FAILED but the route was a compliance
        # substitution, the caller still deserves to know the substitution
        # happened (the substituted provider is the one that failed; not
        # the originally-requested-and-banned one). Merge the disclosure
        # headers onto the error response + rewrite the audit row's
        # http_status to reflect the actual outcome.
        # v5.0.9 — upstream-error disclosure extracted to
        # ``_compliance_handler.disclosure_headers_for_upstream_error``.
        from app.api._compliance_handler import disclosure_headers_for_upstream_error
        error_headers = await disclosure_headers_for_upstream_error(
            request, db, route, key_record, _orig_request_model, status_code,
        )
        raise HTTPException(
            status_code,
            f"Upstream provider error ({cls}): {clean}",
            headers=error_headers or None,
        )


