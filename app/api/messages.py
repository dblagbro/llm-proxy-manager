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
from app.auth.keys import verify_api_key
from app.routing.router import select_provider
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


@router.post("/v1/messages")
async def messages(
    request: Request,
    background_tasks: BackgroundTasks,
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
    key_record = await verify_api_key(db, x_api_key)
    # v3.0.45: set tenant context for select_provider's ownership filter
    # so cascade/critique/hedge/grader paths inherit it without plumbing.
    from app.routing.tenant import current_api_key_id
    current_api_key_id.set(key_record.id)

    # v5.0.0 — compliance UA pre-check (decision 16 + 22). Fires BEFORE
    # any provider routing so banned client products are refused even
    # when the requested model is allowed. v5.0.9 — extracted to
    # ``_compliance_handler.raise_if_banned_client_ua``; same logic,
    # one mirror.
    from app.api._compliance_handler import raise_if_banned_client_ua
    await raise_if_banned_client_ua(request, db, key_record)

    # v5.2.0 / Batch V1 — LLM emergency stop. Fires BEFORE provider
    # selection so a halted fleet doesn't waste a select_provider call.
    # Separate from the v5.1.0 ``activity_logging_enabled`` toggle:
    # that one suppresses log WRITES; this one refuses LLM CALLS.
    # Body isn't parsed yet, so ``requested_model`` is captured later
    # via the audit row's ``X-Compliance-Requested-Model`` header path
    # — the stop is unconditional regardless of model.
    from app.api._compliance_handler import raise_if_llm_emergency_stopped
    await raise_if_llm_emergency_stopped(db, key_record, endpoint="messages")

    # v4.4.15 (F-OBS-003) — record whether the caller-memory gating
    # header is present, so the operator can see when a consumer
    # starts sending it (caller_memory write-back has had 0 prod
    # writes despite the flag being ON since 2026-05-15).
    try:
        from app.observability.prometheus import CONVERSATION_ID_REQUESTS_TOTAL
        CONVERSATION_ID_REQUESTS_TOTAL.labels(
            endpoint="messages",
            has_conversation_id="true" if x_conversation_id else "false",
        ).inc()
    except Exception:
        pass  # telemetry must never break the request path

    # v4.4.23 — set the per-request contextvars so the activity_log
    # row for this request can record verifiable header presence.
    # Surfaced 2026-05-27 by a DevinGPT follow-up asking us to confirm
    # whether two specific historical events had the header — we
    # couldn't, because event_meta never captured it. Counter-only
    # telemetry resets on restart; the contextvar bridges to per-row.
    try:
        from app.observability.request_context import set_caller_memory_headers
        set_caller_memory_headers(
            has_conversation_id=bool(x_conversation_id),
            has_memory_tag=bool(x_memory_tag),
        )
    except Exception:
        pass

    body = await request.json()
    # v5.0.6 — capture the caller's ORIGINAL model name before any
    # rewriting downstream (the v3.0.36 cross-family fallback at
    # line ~355 rewrites ``body["model"]`` to the served-model-native
    # so the claude-oauth dispatcher reads the right slug; that
    # mutation predates v5.0.0 and was correct for its purpose, but
    # the v5.0.0 audit row at line ~557 then read body.get("model")
    # AFTER the mutation, mislabeling the audit's ``requested_model``
    # field with the served model. Same hit the
    # ``X-Compliance-Requested-Model`` response header. Capturing
    # here is the single-point fix — every audit + disclosure site
    # downstream uses ``_orig_request_model`` instead.
    _orig_request_model = body.get("model") if isinstance(body, dict) else None
    # v3.5.8 BUG-005 fix — validate request shape at the input boundary.
    # Pre-fix the proxy treated `{}` and `{"model":"x"}` (no messages)
    # as valid, auto-routed to a default provider, and returned 200 with
    # a substituted model. That's a denial-of-wallet vector if any API
    # key leaks. Now: empty body → 400; missing model → 400; invalid
    # role → 400; negative max_tokens → 400. All BEFORE upstream dispatch.
    from app.api._input_validation import (
        validate_completion_request,
        validate_webhook_url,
    )
    validate_completion_request(body, endpoint="messages")
    validate_webhook_url(x_webhook_url)
    messages_list = body.get("messages", [])
    stream = body.get("stream", False)
    max_tokens = body.get("max_tokens", 1024)
    system = body.get("system")
    thinking = body.get("thinking")
    tools = body.get("tools")

    from app.api._request_pipeline import (
        apply_privacy_filters, build_hint_with_auto_task,
        apply_context_compression, build_base_response_headers,
    )

    messages_list, _pii_masked_count = apply_privacy_filters(messages_list, body)
    hint, auto_task = await build_hint_with_auto_task(llm_hint, messages_list)
    has_tools = bool(tools)
    has_images = has_images_anthropic(messages_list)

    # v3.9.5 (#267 Phase 8) — memory injection moved here from pre-route
    # to post-route so we can gate on route.provider.memory_disabled.
    # The actual inject call site is below, after Phase 6 flush and
    # cross-family body rewrite, but before Fix B translation (which
    # consumes Anthropic-shape body['system']).
    _mem_injected = False

    # v2.8.0: parse :floor / :nitro / :exacto suffix off the requested model.
    # The suffix never reaches upstream — Anthropic / OpenAI etc. would 4xx.
    from app.routing.model_slug import parse_model_slug, is_auto_model
    parsed_slug = parse_model_slug(body.get("model"))
    if parsed_slug.sort_mode is not None:
        body = {**body, "model": parsed_slug.bare_model}

    # v3.0.27: same embedding-on-chat guard as completions.py — embedding
    # models can't dispatch through /v1/messages either.
    from app.routing.router import _is_embedding_model
    if _is_embedding_model(parsed_slug.bare_model):
        raise HTTPException(
            400,
            f"Model {parsed_slug.bare_model!r} is an embeddings model. "
            f"Use POST /v1/embeddings instead of /v1/messages.",
        )

    # v2.8.0: ``model: "auto"`` (and ``"llmp-auto"``) — let LMRH ranking pick
    # the provider AND the model. The auto-task classifier in
    # build_hint_with_auto_task already inferred a task dimension above, so
    # capability scoring has signal even without an explicit hint header.
    is_auto = is_auto_model(parsed_slug.bare_model)
    alias = await resolve_alias(db, body.get("model")) if not is_auto else None
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

    # v3.10.0 (#269 Fix B, widened) — Anthropic→OpenAI body translation.
    # The /v1/messages endpoint always receives an Anthropic-wire body,
    # but litellm's request API is OpenAI-shaped for EVERY provider it
    # dispatches (Gemini, OpenAI, OpenRouter, and even litellm-Anthropic
    # all included). So a request carrying Anthropic content blocks
    # (tool_use / tool_result / image) 400s with "Invalid user message
    # at index N" whenever it reaches a litellm provider untranslated.
    #
    # v3.9.1's original Fix B only translated on ``cross_family_fallback``,
    # which left direct ``/v1/messages`` → Gemini / → OpenRouter tool-using
    # requests broken — the dominant fleet failure class in the
    # 2026-05-15 audit (~69% of all warnings). Translation must run for
    # ANY litellm-dispatched route whose body has content blocks, not
    # just fallbacks.
    #
    # Skipped for: claude-oauth (its own native-Anthropic dispatcher) and
    # tool-emulation (its own Anthropic-shape prompt path — translating
    # here would feed OpenAI-shape messages to normalize_anthropic_messages).
    # BUG-047 (v4.3.8): also fire translation when the request carries
    # Anthropic-shape tool DEFINITIONS at the top level (``body.tools``)
    # but no tool-use/tool-result message blocks yet (first turn). Pre-
    # fix, those defs reached litellm untranslated and 400'd on
    # OpenAI/Cohere/etc. with "missing required field: 'type'" —
    # observed multiple times/day on Devin-Cohere + Devin Personal
    # OpenAI ChatGPT in the 2026-05-20 monitoring sweep.
    _has_anthropic_tool_defs = has_anthropic_tool_defs(body.get("tools"))
    _cross_family_translated = False
    _needs_openai_translation = (
        route.profile.provider_type != "claude-oauth"
        and not route.tool_emulation_engaged
        and (
            route.cross_family_fallback
            or _has_tool_blocks
            or has_images
            or _has_anthropic_tool_defs
        )
    )
    if _needs_openai_translation:
        from app.api._oauth_chat_translate import anthropic_to_openai_body
        translated = anthropic_to_openai_body({
            **body,
            "messages": messages_list,
            "system": system,
            "tools": tools,
        })
        messages_list = translated.get("messages") or []
        system = None  # Folded into messages_list as the leading role:system
        tools = translated.get("tools")
        body = {**body, "messages": messages_list}
        body.pop("system", None)
        if tools is not None:
            body["tools"] = tools
        else:
            body.pop("tools", None)
        _cross_family_translated = True

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
    elif thinking and route.profile.provider_type == "anthropic":
        extra["thinking"] = thinking

    # Forward anthropic-beta header when routing to Anthropic — some cache
    # directives (e.g. 1-hour TTL) require this. No-op for other providers.
    if anthropic_beta and route.profile.provider_type == "anthropic":
        extra["extra_headers"] = {"anthropic-beta": anthropic_beta}

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

                    async def _replay_hedged_stream(_f=_hfirst, _g=racer):
                        yield _f
                        async for _c in _g:
                            yield _c

                    return StreamingResponse(
                        _replay_hedged_stream(),
                        media_type="text/event-stream", headers=resp_headers,
                    )
            elif wait_ms is not None:
                observe_hedge_bucket_reject()

            # v3.10.13 BUG-001 — pre-flight the litellm streaming path so a
            # pre-stream upstream failure (auth, rate-limit, 5xx) surfaces
            # as a real HTTP status instead of a 200 + terminal SSE error
            # frame. Matches the claude-oauth streaming path, which already
            # pre-flights. A mid-stream failure (after message_start) still
            # degrades to an SSE error frame — the 200 is already sent.
            _gen = _stream_anthropic(
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
            _first, _stream_err, _gen = await preflight_sse(_gen)
            if _stream_err is not None:
                await _gen.aclose()
                raise HTTPException(
                    http_status_for_stream_error(_stream_err),
                    f"Upstream error before streaming began: {_stream_err}",
                )

            async def _replay_anthropic_stream(_f=_first, _g=_gen):
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
                elif thinking and r.profile.provider_type == "anthropic":
                    local_extra["thinking"] = thinking
                if anthropic_beta and r.profile.provider_type == "anthropic":
                    local_extra["extra_headers"] = {"anthropic-beta": anthropic_beta}
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


