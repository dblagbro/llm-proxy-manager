"""
/v1/chat/completions — OpenAI-format endpoint (same path as v1).
"""

import json
import logging
import time
from typing import Optional as Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse as StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._completions_streaming import _stream_cot_openai, _webhook_completion_openai
from app.api._completions_streaming import _stream_openai as _stream_openai
from app.api._messages_streaming import buffer_sse_until_content as buffer_sse_until_content
from app.api._messages_streaming import http_status_for_stream_error as http_status_for_stream_error
from app.api._messages_streaming import preflight_sse as preflight_sse
from app.api._messages_streaming import stream_with_empty_guard as stream_with_empty_guard
from app.api.image_utils import has_images_openai, strip_images_openai
from app.cache.middleware import maybe_store
from app.config import settings as _cfg_settings
from app.cot.sse import openai_text_response, openai_text_sse
from app.cot.sse import openai_tool_response as openai_tool_response
from app.cot.sse import openai_tool_sse as openai_tool_sse
from app.cot.sse import openai_tools_response as openai_tools_response
from app.cot.sse import openai_tools_sse as openai_tools_sse
from app.cot.tool_emulation import build_openai_tool_prompt as build_openai_tool_prompt
from app.cot.tool_emulation import call_with_tool_prompt as call_with_tool_prompt
from app.cot.tool_emulation import normalize_openai_messages as normalize_openai_messages
from app.cot.tool_emulation import parse_tool_calls as parse_tool_calls
from app.cot.tool_emulation import strip_thinking as strip_thinking
from app.models.database import get_db
from app.monitoring.helpers import record_outcome
from app.observability.otel import llm_span
from app.observability.prometheus import observe_hedge_attempt as observe_hedge_attempt
from app.observability.prometheus import observe_hedge_bucket_reject as observe_hedge_bucket_reject
from app.observability.prometheus import observe_hedge_win as observe_hedge_win
from app.routing.aliases import resolve_alias
from app.routing.hedging import race_streams as race_streams
from app.routing.hedging import should_hedge_header as should_hedge_header
from app.routing.hedging import try_acquire_hedge as try_acquire_hedge
from app.routing.hedging import wait_budget_ms as wait_budget_ms
from app.routing.litellm_binding import (
    OWN_DISPATCHER_PROVIDER_TYPES as OWN_DISPATCHER_PROVIDER_TYPES,
)
from app.routing.litellm_binding import clamp_thinking_budget
from app.routing.retry import acompletion_with_retry
from app.routing.router import select_provider as select_provider
from app.utils.disconnect_watchdog import watch_for_disconnect

logger = logging.getLogger(__name__)
router = APIRouter()


# pool-leak-audit: watchdog+bounded
# Same rationale as app/api/messages.py::messages — the disconnect
# watchdog releases the session on abort; LLM streams are bounded
# (~60s max) so the session isn't held for the multi-hour class of
# leak that v5.21.7 fixed in runs.py.
@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    background_tasks: BackgroundTasks,
    # v5.7.17 — see app/api/messages.py for the watchdog rationale.
    # v5.21.14 — db BEFORE _watchdog (LIFO cleanup closes get_db last, after
    # the watchdog stops). Prevents disconnect-cancel from leaking a pool slot
    # during session.close(). Same fix as cluster.py v5.21.12. Do NOT reorder.
    db: AsyncSession = Depends(get_db),
    _watchdog: None = Depends(watch_for_disconnect),
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="x-api-key"),
    llm_hint: str | None = Header(None, alias="llm-hint"),
    x_session_id: str | None = Header(None, alias="x-session-id"),
    x_cot_iterations: str | None = Header(None, alias="x-cot-iterations"),
    x_cot_verify: str | None = Header(None, alias="x-cot-verify"),
    x_cot_samples: str | None = Header(None, alias="x-cot-samples"),
    x_cot_mode: str | None = Header(None, alias="x-cot-mode"),
    x_webhook_url: str | None = Header(None, alias="x-webhook-url"),
    x_cache: str | None = Header(None, alias="x-cache"),
    x_cache_ttl: str | None = Header(None, alias="x-cache-ttl"),
    x_hedge: str | None = Header(None, alias="x-hedge"),
    x_context_strategy: str | None = Header(None, alias="x-context-strategy"),
    x_conversation_id: str | None = Header(None, alias="x-conversation-id"),
    x_memory_tag: str | None = Header(None, alias="x-memory-tag"),
):
    # Accept Bearer token or x-api-key (completions.py only — the
    # Anthropic-compatible /v1/messages requires x-api-key).
    token = x_api_key
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    # v5.7.23 — Phase 2 refactor: tenant ctx + compliance UA +
    # emergency stop + telemetry + contextvars all live in the shared
    # helper now (was duplicated verbatim with messages.py). The
    # helper does its own ``verify_api_key`` so we pass the resolved
    # ``token`` instead of repeating the auth dance.
    from app.api._handler_shared import prepare_request_context

    key_record = await prepare_request_context(
        request,
        db,
        token,
        endpoint="completions",
        x_conversation_id=x_conversation_id,
        x_memory_tag=x_memory_tag,
    )

    body = await request.json()
    # v5.7.23 — Phase 2 refactor: shared normalization (input
    # validation + suffix-strip + embedding guard + auto-resolve +
    # alias). Returns the (possibly rebound) body + the four derived
    # values. Behavior unchanged.
    from app.api._handler_shared import normalize_request_body

    body, _orig_request_model, parsed_slug, is_auto, alias = await normalize_request_body(
        body,
        x_webhook_url,
        db,
        endpoint="completions",
    )
    messages_list = body.get("messages", [])
    stream = body.get("stream", False)
    tools = body.get("tools")

    from app.api._request_pipeline import (
        apply_context_compression,
        apply_privacy_filters,
        build_base_response_headers,
        build_hint_with_auto_task,
    )

    messages_list, _pii_masked_count = apply_privacy_filters(messages_list, body)
    hint, auto_task = await build_hint_with_auto_task(llm_hint, messages_list)
    has_tools = bool(tools)
    has_images = has_images_openai(messages_list)

    # v3.9.5 (#267 Phase 8) — memory injection moved from here to
    # post-route-selection so we can gate on route.provider.memory_disabled.
    _mem_injected = False

    # v5.7.23 — parse_model_slug + is_auto_model + embedding guard
    # moved into ``normalize_request_body`` above. parsed_slug,
    # is_auto, alias unpacked at the call site.
    # v3.0.27 inline rationale (preserved): reject embedding-only model
    # names at chat entry. Cohere's chat API returns 400 on `embed-*`
    # slugs; OpenAI does the same on `text-embedding-*`. Misroute
    # discovered when Devin-Cohere's default_model (embed-english-v3.0)
    # was reached via the default-fallthrough path on a chat call.
    # a clear pointer than let it fail upstream.
    from app.routing.router import _is_embedding_model

    if _is_embedding_model(parsed_slug.bare_model):
        raise HTTPException(
            400,
            f"Model {parsed_slug.bare_model!r} is an embeddings model. "
            f"Use POST /v1/embeddings instead of /v1/chat/completions.",
        )
    alias = await resolve_alias(db, body.get("model")) if not is_auto else None
    # v5.22.47 — Daybreak Blue (OpenAI Trusted Access for Cyber) gate. Opt-in
    # ONLY via the `daybreak`/`daybreak-blue` alias; authorized keys only; Red
    # hard-blocked; audited. Returns the access_programs dict to inject into the
    # Codex Responses body, or None for all normal traffic. The provider pin +
    # model resolution ride the existing alias path (the `daybreak` ModelAlias
    # row → approved provider, model_id codex-auto-review).
    from app.providers.daybreak import daybreak_gate

    _daybreak_access_programs = await daybreak_gate(
        db,
        requested_model=body.get("model"),
        key_record=key_record,
        alias=alias,
        client_user_agent=request.headers.get("user-agent"),
    )
    if _daybreak_access_programs is not None and alias is not None:
        # The Codex dispatch sends body["model"] upstream; rewrite the alias
        # name to the approved account's real slug so it isn't rejected.
        body = {**body, "model": alias.model_id}
    # v3.0.x refactor: provider selection + 503 conversion + auto-model
    # resolution moved into _request_pipeline shared helpers. Both
    # /v1/messages and /v1/chat/completions go through the same code path
    # here — prevents the kind of divergence that caused v3.0.99's
    # gemini-routing-to-claude-oauth bug.
    #
    # Note: completions.py uses the generic 503 (detailed_503=False);
    # messages.py uses the actionable variant.
    # v3.0.38 still applies: claude-oauth is reachable from /v1/chat/completions
    # via the OpenAI↔Anthropic wire-format translator — no exclusion needed.
    from app.api._compliance_handler import raise_for_no_substitute_exception
    from app.api._request_pipeline import (
        resolve_auto_model_into_body,
        select_provider_with_503,
    )

    # v5.0.0 → v5.0.9 — `ComplianceNoSubstituteError` / `…NoLocalProviderError`
    # → 503 conversion extracted to ``_compliance_handler.raise_for_no_substitute_exception``.
    from app.compliance import ComplianceNoSubstituteError

    try:
        route = await select_provider_with_503(
            db,
            hint,
            has_tools=has_tools,
            has_images=has_images,
            key_record=key_record,
            parsed_slug=parsed_slug,
            alias=alias,
            detailed_503=False,
            messages=body.get("messages"),
            # v5.22.13 — see messages.py; OpenAI shape carries max_tokens
            # (and max_completion_tokens on newer clients).
            requested_max_tokens=(body.get("max_tokens") or body.get("max_completion_tokens")),
        )
    except ComplianceNoSubstituteError as _exc:
        await raise_for_no_substitute_exception(
            _exc,
            request=request,
            db=db,
            key_record=key_record,
            orig_request_model=_orig_request_model,
        )
    body = resolve_auto_model_into_body(body, route, is_auto)
    # Kept as a local for downstream record_outcome calls (lines ~600-615).
    requested_model = (alias.model_id if alias else parsed_slug.bare_model) or None

    # v3.8.9 (#267) Phase 4 — proxy-side caller-memory injection.
    # Relocated here in v3.9.5 (Phase 8) so we can gate on the chosen
    # provider's memory_disabled flag. Silent degrade on any store error.
    if not getattr(route.provider, "memory_disabled", False):
        from app.memory.inject import maybe_inject_memory

        body, _mem_injected = await maybe_inject_memory(
            db,
            body=body,
            api_key_id=key_record.id,
            conversation_id=x_conversation_id,
            memory_tag=x_memory_tag,
            endpoint="completions",
        )
        if _mem_injected:
            messages_list = body.get("messages", messages_list)

    # OTEL GenAI span: routing-decision metadata (no-op if OTLP endpoint unset)
    with llm_span(
        operation="chat",
        provider_type=route.profile.provider_type,
        requested_model=body.get("model") or "",
        resolved_model=route.litellm_model,
        lmrh_hint=llm_hint,
        cot_engaged=route.cot_engaged,
        unmet_hints=route.unmet_hints,
    ):
        pass

    extra = {**route.litellm_kwargs}
    if tools:
        extra["tools"] = tools
    if body.get("max_tokens"):
        extra["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        extra["temperature"] = body["temperature"]
    # Native reasoning: inject router-computed params; allow per-request reasoning_effort override
    if route.native_thinking_params:
        extra.update(route.native_thinking_params)
        # v5.18.1 (#512 additive) — reasoning_effort can now also come
        # from the ``x-llmproxy-config`` blob. Body wins over blob
        # (matches the general precedence pattern documented in the
        # config-header module). Empty string is treated as absent.
        _re_from_body = body.get("reasoning_effort")
        _re_from_blob = None
        if not _re_from_body:
            try:
                from app.api._llmproxy_config_header import read_config_key

                _re_from_blob = read_config_key(request, "reasoning_effort")
            except Exception:
                pass
        _re_effective = _re_from_body or _re_from_blob
        if "reasoning_effort" in route.native_thinking_params and _re_effective:
            extra["reasoning_effort"] = _re_effective
        # v5.3.7 — keep Gemini thinking budget below max_tokens (empty-success fix)
        clamp_thinking_budget(extra)

    vision_routed_count = 0
    if route.vision_stripped:
        if _cfg_settings.vision_route_enabled:
            from app.api.vision_route import transcribe_openai

            messages_list, vision_routed_count = await transcribe_openai(
                messages_list,
                db,
                exclude_provider_id=route.provider.id,
            )
        else:
            messages_list = strip_images_openai(messages_list)

    messages_list, context_strategy_applied = await apply_context_compression(
        messages_list,
        route=route,
        x_context_strategy=x_context_strategy,
        extra=extra,
        system="",
    )

    budget_total = body.get("max_tokens", 0) or 0
    resp_headers = build_base_response_headers(
        route=route,
        auto_task=auto_task,
        vision_routed_count=vision_routed_count,
        context_strategy_applied=context_strategy_applied,
        pii_masked_count=_pii_masked_count,
        hint=hint,
        max_tokens=None,  # OpenAI endpoint doesn't emit X-Token-Budget-Remaining
    )
    if budget_total:
        resp_headers["X-Token-Budget-Remaining"] = str(budget_total)
    # v2.8.0 — slug-shortcut + auto-routing decision visibility
    if parsed_slug.sort_mode:
        resp_headers["X-Sort-Mode"] = parsed_slug.sort_mode
    if is_auto:
        resp_headers["X-Auto-Routed"] = f"{route.provider.name}:{route.profile.model_id}"
    if _mem_injected:
        resp_headers["X-Caller-Memory"] = "injected"
    # Budget visibility headers (soft-cap warning, remaining $ today/this hour)
    if key_record.budget_status is not None:
        from app.budget.tracker import warnings_for

        resp_headers.update(warnings_for(key_record.budget_status))

    # v5.0.0 → v5.0.9 — substitution disclosure extracted to
    # ``_compliance_handler.emit_substitution_disclosure_for_route``.
    from app.api._compliance_handler import emit_substitution_disclosure_for_route

    (
        _headers_to_merge,
        _compliance_disclosure,
        _compliance_wants_sse_prelude,
    ) = await emit_substitution_disclosure_for_route(
        request,
        db,
        route,
        key_record,
        _orig_request_model,
    )
    if _headers_to_merge:
        resp_headers.update(_headers_to_merge)

    # ── v5.22.4 RELEASE BOUNDARY (connection-hold leak fix, option A) ───────
    # Symmetric to messages.py: all pre-dispatch DB work is done; commit to
    # return the pooled connection BEFORE the upstream dispatch/stream so the
    # open read-transaction doesn't pin it for the whole request. Session
    # stays usable (re-acquires lazily); record_outcome / memory-extract
    # self-manage their own short sessions.
    try:
        await db.commit()
    except Exception:
        pass

    # v3.0.36: cross-family fallback — rewrite body['model'] to the resolved
    # served model so dispatchers that read body['model'] (codex-oauth,
    # claude-oauth) send the right slug upstream. The original requested
    # model is reflected in the LLM-Capability response header.
    if route.cross_family_fallback and route.served_model_native:
        body = {**body, "model": route.served_model_native}

    # v3.0.15: codex-oauth providers bypass the rest of the litellm pipeline
    # (no semantic cache, no CoT, no tool emulation, no fallback chain — same
    # short-circuit pattern as claude-oauth). Translate Chat Completions ↔
    # Responses API and forward to chatgpt.com/backend-api/codex/responses
    # with the OAuth bearer + ChatGPT-Account-ID workspace header.
    if route.provider.provider_type == "ChatGPT-oauth-plan":
        from app.api._codex_oauth_dispatch import dispatch_codex_oauth

        return await dispatch_codex_oauth(
            provider=route.provider,
            body=body,
            stream=stream,
            db=db,
            resp_headers=resp_headers,
            # v3.0.97 — pass through caller identity + hint so dispatch
            # can call record_outcome with the right tenant + lmrh_hint.
            key_record_id=key_record.id,
            llm_hint=llm_hint,
            # v5.22.47 — Daybreak Blue cyber tier (None for normal traffic).
            access_programs=_daybreak_access_programs,
        )

    # v5.22.37 — dispatch extracted to ``_completions_claude_oauth``; see that
    # module for the original rationale. Same shape as the codex-oauth and
    # grok-web branches above/below.
    if route.provider.provider_type == "claude-oauth":
        from app.api._completions_claude_oauth import dispatch_claude_oauth_openai

        return await dispatch_claude_oauth_openai(
            route=route,
            body=body,
            stream=stream,
            db=db,
            key_record=key_record,
            llm_hint=llm_hint,
            resp_headers=resp_headers,
            x_conversation_id=x_conversation_id,
            x_memory_tag=x_memory_tag,
        )

    # v5.22.37 — dispatch + failover extracted to ``_completions_grok_web``;
    # see that module for the original rationale and the (response, route)
    # contract. ``extra`` and ``resp_headers`` are mutated in place there.
    if route.provider.provider_type == "grok-web":
        from app.api._completions_grok_web import dispatch_grok_web_with_failover

        _gw_resp, route = await dispatch_grok_web_with_failover(
            route=route,
            body=body,
            stream=stream,
            db=db,
            hint=hint,
            key_record=key_record,
            extra=extra,
            resp_headers=resp_headers,
            llm_hint=llm_hint,
        )
        if _gw_resp is not None:
            return _gw_resp
        # else: fell over to `route`; continue down the litellm path below.
        # Fall through to the litellm dispatch path below with the
        # new route.

    # Semantic cache — check before anything LLM-ish runs.
    # v3.5.x R1 (2026-05-09): orchestration extracted to
    # _request_pipeline.maybe_serve_from_cache. See messages.py for
    # the rationale; this is the OpenAI-shape variant — passes
    # ``system=None`` (OpenAI puts system in messages[0]) and the
    # openai_text_* response builders.
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
        system=None,
        tools=tools,
        x_cache_ttl_header=x_cache_ttl,
        tenant_id=key_record.id,
        endpoint="completions",
        text_sse_fn=openai_text_sse,
        text_response_fn=openai_text_response,
        resp_headers=resp_headers,
        stream=stream,
    )
    if cache_resp is not None:
        return cache_resp

    # Webhook async: fire-and-forget completion, return 202 immediately
    if x_webhook_url:
        background_tasks.add_task(
            _webhook_completion_openai,
            x_webhook_url,
            route.litellm_model,
            messages_list,
            extra,
            route.provider.id,
            db,
            key_record.id,
        )
        return JSONResponse(
            {"status": "queued", "webhook_url": x_webhook_url},
            status_code=202,
            headers=resp_headers,
        )

    try:
        if route.tool_emulation_engaged:
            from app.api._completions_tool_emulation import dispatch_tool_emulation

            return await dispatch_tool_emulation(
                body=body,
                tools=tools,
                route=route,
                messages_list=messages_list,
                extra=extra,
                db=db,
                key_record=key_record,
                resp_headers=resp_headers,
                stream=stream,
            )

        # CoT-E engagement.
        # v3.5.x R2 (2026-05-09): orchestration extracted to
        # _request_pipeline.maybe_engage_cot. The OpenAI flow uses
        # _stream_cot_openai with the standard arg list — no extras.
        from app.api._request_pipeline import maybe_engage_cot

        cot_resp = await maybe_engage_cot(
            route=route,
            stream=stream,
            db=db,
            key_record=key_record,
            hint=hint,
            body=body,
            messages_list=messages_list,
            extra=extra,
            x_cot_iterations=x_cot_iterations,
            x_cot_verify=x_cot_verify,
            x_cot_samples=x_cot_samples,
            x_cot_mode=x_cot_mode,
            x_session_id=x_session_id,
            resp_headers=resp_headers,
            stream_cot_fn=_stream_cot_openai,
        )
        if cot_resp is not None:
            return cot_resp

        if stream:
            from app.api._completions_stream_dispatch import dispatch_stream

            return await dispatch_stream(
                route=route,
                hint=hint,
                x_hedge=x_hedge,
                db=db,
                has_tools=has_tools,
                has_images=has_images,
                key_record=key_record,
                messages_list=messages_list,
                extra=extra,
                budget_total=budget_total,
                cache_decision=cache_decision,
                _compliance_disclosure=_compliance_disclosure,
                _compliance_wants_sse_prelude=_compliance_wants_sse_prelude,
                tools=tools,
                body=body,
                resp_headers=resp_headers,
                alias=alias,
                is_auto=is_auto,
                requested_model=requested_model,
            )
        else:
            t0 = time.monotonic()

            # Wave 5 #24 — structured output repair loop for response_format
            if _cfg_settings.structured_output_enabled and not has_tools:
                from app.cot.structured_output import call_with_schema, extract_openai_schema

                schema = extract_openai_schema(body)
                if schema is not None:
                    parsed, raw_text, attempts = await call_with_schema(
                        model=route.litellm_model,
                        messages=messages_list,
                        schema=schema,
                        extra=extra,
                        max_repairs=_cfg_settings.structured_output_max_repairs,
                    )
                    resp_headers["X-Structured-Output-Attempts"] = str(attempts)
                    resp_headers["X-Structured-Output-Status"] = (
                        "valid" if parsed is not None else "invalid"
                    )
                    final_text = json.dumps(parsed) if parsed is not None else raw_text
                    await record_outcome(
                        db,
                        route.provider.id,
                        route.litellm_model,
                        endpoint="completions",
                        success=True,
                        t0=t0,
                        key_record_id=key_record.id,
                        provider_name=route.provider.name,
                    )
                    try:
                        await maybe_store(cache_decision, final_text)
                    except Exception:
                        pass
                    # Build an OpenAI-format response manually so the output
                    # is exactly the validated JSON (no wrapper fences).
                    return JSONResponse(
                        content={
                            "id": f"chatcmpl-struct-{int(time.monotonic() * 1000)}",
                            "object": "chat.completion",
                            "model": route.litellm_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": final_text},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 0,
                                "completion_tokens": 0,
                                "total_tokens": 0,
                            },
                        },
                        headers=resp_headers,
                    )

            # Wave 3 #17 — ordered fallback across ranked providers
            from app.routing.fallback import try_ranked_non_streaming

            async def _call_with_route(r):
                local_extra = {**r.litellm_kwargs}
                if tools:
                    local_extra["tools"] = tools
                if body.get("max_tokens"):
                    local_extra["max_tokens"] = body["max_tokens"]
                if body.get("temperature") is not None:
                    local_extra["temperature"] = body["temperature"]
                if r.native_thinking_params:
                    local_extra.update(r.native_thinking_params)
                    if "reasoning_effort" in r.native_thinking_params and body.get(
                        "reasoning_effort"
                    ):
                        local_extra["reasoning_effort"] = body["reasoning_effort"]
                    clamp_thinking_budget(local_extra)
                return await acompletion_with_retry(
                    model=r.litellm_model,
                    messages=messages_list,
                    stream=False,
                    **local_extra,
                )

            if _cfg_settings.fallback_enabled:
                result, final_route, chain = await try_ranked_non_streaming(
                    db,
                    hint,
                    has_tools=has_tools,
                    has_images=has_images,
                    key_type=key_record.key_type,
                    pinned_provider_id=alias.provider_id if alias else None,
                    model_override=alias.model_id if alias else None,
                    primary_route=route,
                    call_fn=_call_with_route,
                )
                if len(chain.attempts) > 1:
                    resp_headers["X-Fallback-Chain"] = chain.as_header()
                    resp_headers["X-Provider"] = final_route.provider.name
                    resp_headers["X-Resolved-Model"] = final_route.litellm_model
                    route = final_route
            else:
                result = await acompletion_with_retry(
                    model=route.litellm_model,
                    messages=messages_list,
                    stream=False,
                    **extra,
                )
            in_tok = getattr(result.usage, "prompt_tokens", 0)
            out_tok = getattr(result.usage, "completion_tokens", 0)
            try:
                answer_text = result.choices[0].message.content or ""
                await maybe_store(cache_decision, answer_text)
            except Exception:
                pass
            await record_outcome(
                db,
                route.provider.id,
                route.litellm_model,
                endpoint="completions",
                success=True,
                in_tok=in_tok,
                out_tok=out_tok,
                t0=t0,
                key_record_id=key_record.id,
                provider_name=route.provider.name,
                # v3.0.35: body capture + diagnostic fields for self-serve
                # activity-log queries. Backwards-compat: extras are skipped
                # when capture is disabled and absent fields are absent.
                request_body=body,
                response_body=result.model_dump() if hasattr(result, "model_dump") else None,
                requested_model=requested_model,
                had_lmrh_hint=bool(llm_hint),
                lmrh_hint_raw=llm_hint or None,
                # v3.8.3 (#263) — tool-call telemetry on native path.
                tool_call_format=("native" if has_tools else None),
            )
            if budget_total:
                resp_headers["X-Token-Budget-Remaining"] = str(max(0, budget_total - out_tok))
            # v3.6.1 — X-Quality-Hint thin-content detector
            from app.api._quality_hint import merge_into_headers

            _result_body = result.model_dump()
            # v5.0.24 / Batch 3 — empty-success guard (BUG-053). Some
            # bridges (cursor-bridge confirmed; pattern likely broader)
            # wrap upstream errors as HTTP 200 with empty content +
            # zero tokens. Detect that pattern and treat as a real
            # upstream failure so the routing layer can record + skip.
            # See app/api/_response_validators.py for the heuristic.
            from app.api._response_validators import (
                empty_success_failure_message,
                looks_like_empty_success_failure,
            )

            if looks_like_empty_success_failure(response_dict=_result_body):
                msg = empty_success_failure_message(_result_body)
                logger.warning(
                    "completions.empty_success_blocked provider=%s err=%s",
                    route.provider.name,
                    msg,
                )
                # Record failure + trip CB so subsequent requests skip
                # this provider during the cool-off window.
                try:
                    from app.routing.circuit_breaker import record_failure

                    await record_failure(route.provider.id, billing_error=False)
                except Exception:
                    pass
                raise HTTPException(502, f"upstream: {msg}")
            merge_into_headers(resp_headers, _result_body, endpoint="completions")
            # v5.10.0 Ship 1 — emit X-Proxy-MCP-Suggestion when this
            # caller's accumulated score crosses the threshold. Same
            # contract as the messages.py path.
            try:
                from app.capability_scout.suggestion_emit import apply_suggestion_header

                await apply_suggestion_header(db, key_record.id, resp_headers)
            except Exception:
                pass
            # v5.12.2 Ship 3 — accept handler. See messages.py for contract.
            # v5.16.0 (#512) — also read via consolidated x-llmproxy-config
            # blob; individual header wins on precedence.
            try:
                from app.api._llmproxy_config_header import read_config_key
                from app.capability_scout.accept_handler import process_accept_header

                _accept_value = read_config_key(
                    request,
                    "accept_mcp",
                    header_fallback="X-Proxy-Accept-MCP",
                )
                if _accept_value is not None:
                    if isinstance(_accept_value, list):
                        _accept_value = ",".join(str(x) for x in _accept_value)
                    elif not isinstance(_accept_value, str):
                        _accept_value = str(_accept_value)
                    if _accept_value:
                        await process_accept_header(db, key_record.id, _accept_value, resp_headers)
            except Exception:
                pass
            # v5.16.0 — echo parsed config blob.
            try:
                from app.api._llmproxy_config_header import emit_config_applied_header

                emit_config_applied_header(resp_headers, request)
            except Exception:
                pass
            # v5.14.0 — response-hook runner. See messages.py for contract.
            try:
                from app.api._response_hook_runner import HookContext, apply_response_hooks

                await apply_response_hooks(
                    handler_id="completions",
                    resp_headers=resp_headers,
                    context=HookContext(
                        requested_model=body.get("model") if isinstance(body, dict) else None,
                        served_model=getattr(route, "litellm_model", None),
                        api_key_id=getattr(key_record, "id", None),
                        provider_id=getattr(route.provider, "id", None)
                        if hasattr(route, "provider")
                        else None,
                        substituted=bool(getattr(route, "compliance_substituted", False)),
                        key_record=key_record,
                        request=request,
                    ),
                )
            except Exception:
                pass
            return JSONResponse(content=_result_body, headers=resp_headers)

    except Exception as e:
        err_str = str(e)
        # v5.22.57: attribute the ERROR row to the provider that actually
        # failed when the fallback chain fell through to a different provider.
        # Absent attribute (fallback disabled / primary raised) → ``route``.
        failed_route = getattr(e, "_llmproxy_attributed_route", None) or route
        await record_outcome(
            db,
            failed_route.provider.id,
            failed_route.litellm_model,
            endpoint="completions",
            success=False,
            key_record_id=key_record.id,
            error_str=err_str,
            provider_name=failed_route.provider.name,
            request_body=body,
            requested_model=requested_model,
            had_lmrh_hint=bool(llm_hint),
            lmrh_hint_raw=llm_hint or None,
        )
        # v3.5.8 BUG-007/008 fix — sanitize before sending to client.
        from app.api._input_validation import sanitize_upstream_error
        from app.routing.circuit_breaker import classify_error

        cls = classify_error(err_str or "")
        clean = sanitize_upstream_error(err_str)
        status_code = 400 if cls == "bad_request" else 502
        # v5.0.0 — preserve compliance disclosure on upstream-error 502. The
        # substitution decision is real even if the substituted provider
        # subsequently fails; caller deserves the headers so they know
        # which provider was tried under what policy.
        # v5.0.9 — upstream-error disclosure extracted to
        # ``_compliance_handler.disclosure_headers_for_upstream_error``.
        from app.api._compliance_handler import disclosure_headers_for_upstream_error

        error_headers = await disclosure_headers_for_upstream_error(
            request,
            db,
            route,
            key_record,
            _orig_request_model,
            status_code,
        )
        raise HTTPException(
            status_code,
            f"Upstream provider error ({cls}): {clean}",
            headers=error_headers or None,
        )
