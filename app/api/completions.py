"""
/v1/chat/completions — OpenAI-format endpoint (same path as v1).
"""
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Request, Depends, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.auth.keys import verify_api_key
from app.routing.router import select_provider
from app.routing.litellm_binding import clamp_thinking_budget
from app.monitoring.helpers import record_outcome
from app.api.image_utils import has_images_openai, strip_images_openai
from app.routing.aliases import resolve_alias
from app.cot.tool_emulation import (
    build_openai_tool_prompt,
    normalize_openai_messages,
    parse_tool_calls,
    call_with_tool_prompt,
    strip_thinking,
)
from app.cot.sse import (
    openai_tool_sse,
    openai_tools_sse,
    openai_text_sse,
    openai_tool_response,
    openai_tools_response,
    openai_text_response,
)
from app.api._completions_streaming import (
    _stream_cot_openai, _stream_openai, _webhook_completion_openai,
)
from app.api._messages_streaming import preflight_sse, http_status_for_stream_error
from app.routing.retry import acompletion_with_retry
from app.observability.otel import llm_span
from app.cache.middleware import maybe_store
from app.routing.hedging import (
    should_hedge_header, wait_budget_ms, race_streams, try_acquire_hedge,
)
from app.observability.prometheus import (
    observe_hedge_attempt, observe_hedge_win, observe_hedge_bucket_reject,
)
from app.config import settings as _cfg_settings

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    llm_hint: Optional[str] = Header(None, alias="llm-hint"),
    x_session_id: Optional[str] = Header(None, alias="x-session-id"),
    x_cot_iterations: Optional[str] = Header(None, alias="x-cot-iterations"),
    x_cot_verify: Optional[str] = Header(None, alias="x-cot-verify"),
    x_cot_samples: Optional[str] = Header(None, alias="x-cot-samples"),
    x_cot_mode: Optional[str] = Header(None, alias="x-cot-mode"),
    x_webhook_url: Optional[str] = Header(None, alias="x-webhook-url"),
    x_cache: Optional[str] = Header(None, alias="x-cache"),
    x_cache_ttl: Optional[str] = Header(None, alias="x-cache-ttl"),
    x_hedge: Optional[str] = Header(None, alias="x-hedge"),
    x_context_strategy: Optional[str] = Header(None, alias="x-context-strategy"),
    x_conversation_id: Optional[str] = Header(None, alias="x-conversation-id"),
    x_memory_tag: Optional[str] = Header(None, alias="x-memory-tag"),
):
    # Accept Bearer token or x-api-key
    token = x_api_key
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    key_record = await verify_api_key(db, token)
    # v3.0.45: tenant context for ownership filter (covers internal call sites)
    from app.routing.tenant import current_api_key_id
    current_api_key_id.set(key_record.id)

    # v5.0.0 → v5.0.9 — compliance UA pre-check extracted to
    # ``_compliance_handler.raise_if_banned_client_ua``.
    from app.api._compliance_handler import raise_if_banned_client_ua
    await raise_if_banned_client_ua(request, db, key_record)

    # v5.2.0 / Batch V1 — LLM emergency stop. See messages.py for
    # rationale + symmetry.
    from app.api._compliance_handler import raise_if_llm_emergency_stopped
    await raise_if_llm_emergency_stopped(db, key_record, endpoint="completions")

    # v4.4.15 (F-OBS-003) — caller-memory gating-header visibility.
    # See messages.py for the rationale.
    try:
        from app.observability.prometheus import CONVERSATION_ID_REQUESTS_TOTAL
        CONVERSATION_ID_REQUESTS_TOTAL.labels(
            endpoint="completions",
            has_conversation_id="true" if x_conversation_id else "false",
        ).inc()
    except Exception:
        pass  # telemetry must never break the request path

    # v4.4.23 — per-request header-presence contextvars, mirror of
    # the equivalent block in messages.py. See request_context.py for
    # the rationale (DevinGPT 2026-05-27 follow-up).
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
    # rewriting downstream. See messages.py:168 for the full
    # rationale. The v3.0.36 cross-family-fallback rewrite at line
    # ~290 mutates body["model"] to the served-model-native, so audit
    # writes downstream that read body.get("model") get the served
    # model. Capturing here is the single-point fix.
    _orig_request_model = body.get("model") if isinstance(body, dict) else None
    # v3.5.8 BUG-004 fix — validate request shape at the input boundary
    # so missing model/messages return 400 instead of cascading to a
    # 502 with upstream error leakage. See _input_validation.py +
    # docs/bug-log.md BUG-004 for the rationale.
    from app.api._input_validation import (
        validate_completion_request,
        validate_webhook_url,
    )
    validate_completion_request(body, endpoint="completions")
    validate_webhook_url(x_webhook_url)
    messages_list = body.get("messages", [])
    stream = body.get("stream", False)
    tools = body.get("tools")

    from app.api._request_pipeline import (
        apply_privacy_filters, build_hint_with_auto_task,
        apply_context_compression, build_base_response_headers,
    )

    messages_list, _pii_masked_count = apply_privacy_filters(messages_list, body)
    hint, auto_task = await build_hint_with_auto_task(llm_hint, messages_list)
    has_tools = bool(tools)
    has_images = has_images_openai(messages_list)

    # v3.9.5 (#267 Phase 8) — memory injection moved from here to
    # post-route-selection so we can gate on route.provider.memory_disabled.
    _mem_injected = False

    # v2.8.0: parse :floor / :nitro / :exacto suffix + auto-routing alias.
    from app.routing.model_slug import parse_model_slug, is_auto_model
    parsed_slug = parse_model_slug(body.get("model"))
    if parsed_slug.sort_mode is not None:
        body = {**body, "model": parsed_slug.bare_model}

    is_auto = is_auto_model(parsed_slug.bare_model)
    # v3.0.27: reject embedding-only model names at chat entry. Cohere's
    # chat API returns 400 on `embed-*` slugs; OpenAI does the same on
    # `text-embedding-*`. Misroute discovered when Devin-Cohere's
    # default_model (embed-english-v3.0) was reached via the
    # default-fallthrough path on a chat call. Better to 400 here with
    # a clear pointer than let it fail upstream.
    from app.routing.router import _is_embedding_model
    if _is_embedding_model(parsed_slug.bare_model):
        raise HTTPException(
            400,
            f"Model {parsed_slug.bare_model!r} is an embeddings model. "
            f"Use POST /v1/embeddings instead of /v1/chat/completions.",
        )
    alias = await resolve_alias(db, body.get("model")) if not is_auto else None
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
    from app.api._request_pipeline import (
        select_provider_with_503, resolve_auto_model_into_body,
    )
    # v5.0.0 → v5.0.9 — `ComplianceNoSubstituteError` / `…NoLocalProviderError`
    # → 503 conversion extracted to ``_compliance_handler.raise_for_no_substitute_exception``.
    from app.compliance import ComplianceNoSubstituteError
    from app.api._compliance_handler import raise_for_no_substitute_exception
    try:
        route = await select_provider_with_503(
            db, hint,
            has_tools=has_tools, has_images=has_images,
            key_record=key_record, parsed_slug=parsed_slug, alias=alias,
            detailed_503=False,
            messages=body.get("messages"),
        )
    except ComplianceNoSubstituteError as _exc:
        await raise_for_no_substitute_exception(
            _exc, request=request, db=db, key_record=key_record,
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
            db, body=body, api_key_id=key_record.id,
            conversation_id=x_conversation_id, memory_tag=x_memory_tag,
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
        if "reasoning_effort" in route.native_thinking_params and body.get("reasoning_effort"):
            extra["reasoning_effort"] = body["reasoning_effort"]
        # v5.3.7 — keep Gemini thinking budget below max_tokens (empty-success fix)
        clamp_thinking_budget(extra)

    vision_routed_count = 0
    if route.vision_stripped:
        if _cfg_settings.vision_route_enabled:
            from app.api.vision_route import transcribe_openai
            messages_list, vision_routed_count = await transcribe_openai(
                messages_list, db, exclude_provider_id=route.provider.id,
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
    _headers_to_merge, _compliance_disclosure, _compliance_wants_sse_prelude = (
        await emit_substitution_disclosure_for_route(
            request, db, route, key_record, _orig_request_model,
        )
    )
    if _headers_to_merge:
        resp_headers.update(_headers_to_merge)

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
            provider=route.provider, body=body, stream=stream, db=db,
            resp_headers=resp_headers,
            # v3.0.97 — pass through caller identity + hint so dispatch
            # can call record_outcome with the right tenant + lmrh_hint.
            key_record_id=key_record.id,
            llm_hint=llm_hint,
        )

    # v3.0.38: claude-oauth on /v1/chat/completions via OpenAI↔Anthropic
    # wire-format translation. DevinGPT ask 2026-05-01: their stack speaks
    # OpenAI ChatCompletion only; this lets them reach Devin-Anthropic-Max-VG
    # without a 600-LOC client-side branch for /v1/messages.
    if route.provider.provider_type == "claude-oauth":
        from app.api._oauth_chat_translate import (
            openai_request_to_anthropic, anthropic_response_to_openai,
            stream_anthropic_to_openai_sse,
        )
        from app.api._messages_streaming import (
            _complete_claude_oauth, _stream_claude_oauth,
        )
        from app.api._cache_inject import (
            inject_cache_control, parse_cache_mode, resolve_min_chars,
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
                build_cache_disclosure, append_cache_disclosure,
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

    # v3.2.0: grok-web on /v1/chat/completions. Operator's grok.com web
    # subscription. v3.2.9 extracted dispatch into a shared module; see
    # _grok_web_dispatch.dispatch_grok_web_openai.
    # v5.0.23 / Batch 2.5 — failover wiring (see messages.py for the
    # symmetric comment + decision rationale).
    if route.provider.provider_type == "grok-web":
        from app.api._grok_web_dispatch import dispatch_grok_web_openai
        gw_resp = await dispatch_grok_web_openai(
            route=route, body=body, stream=stream, resp_headers=resp_headers,
            db=db, key_record_id=key_record.id, t0=time.monotonic(),
            llm_hint=llm_hint,
        )
        if gw_resp is not None:
            return gw_resp
        # Failover — re-resolve excluding the failed grok-web provider.
        from app.routing.router import select_provider
        failed_id = route.provider.id
        new_route = await select_provider(
            db=db, hint=hint,
            has_tools=False, has_images=False,
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
        # failover. select_provider sets cross_family_fallback +
        # rewrites litellm_model to OpenRouter's default
        # (openai/gpt-4o) when the OpenRouter provider's capability
        # scan doesn't list grok-3. For grok-web → openrouter we
        # want the original model (build_litellm_model maps
        # `grok-3` to `openrouter/x-ai/grok-3`). Clear the flag AND
        # rebuild litellm_model from the original model.
        new_route.cross_family_fallback = False
        new_route.served_model_native = None
        from app.routing.litellm_binding import build_litellm_model as _bld
        new_route.litellm_model = _bld(new_route.provider, body.get("model"))
        # v5.1.0 / Batch A4 — swap ``extra`` (litellm_kwargs) to the
        # new provider's. Pre-fix, the existing ``extra`` was built
        # from the grok-web provider's kwargs (no api_key for the
        # OpenRouter call); the litellm dispatch silently fell back
        # to whatever litellm could resolve, served openai/gpt-4o.
        # Mirrors the claude-oauth → litellm chain swap pattern
        # (messages.py:471).
        for _k in list(route.litellm_kwargs.keys()):
            extra.pop(_k, None)
        extra.update(new_route.litellm_kwargs)
        route = new_route
        resp_headers["X-Grok-Web-Failover"] = "true"
        resp_headers["X-Grok-Web-Failover-Target"] = new_route.provider.provider_type
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
            x_webhook_url, route.litellm_model, messages_list, extra,
            route.provider.id, db, key_record.id,
        )
        return JSONResponse(
            {"status": "queued", "webhook_url": x_webhook_url},
            status_code=202,
            headers=resp_headers,
        )

    try:
        if route.tool_emulation_engaged:
            # Wave 5 #23 — respect parallel_tool_calls=false from body
            allow_parallel = body.get("parallel_tool_calls", True) is not False
            # v4.1.1 — co-emulation: reasoning-prefix the tool prompt when
            # CoT-E is also engaged so tools + reasoning are served together.
            tool_prompt = build_openai_tool_prompt(
                tools or [], allow_parallel=allow_parallel,
                with_reasoning=route.cot_engaged,
            )
            norm_msgs = normalize_openai_messages(messages_list)
            if norm_msgs and norm_msgs[0]["role"] == "system":
                norm_msgs[0]["content"] = tool_prompt + "\n\n" + norm_msgs[0]["content"]
            else:
                norm_msgs = [{"role": "system", "content": tool_prompt}] + norm_msgs
            emul_extra = {k: v for k, v in extra.items() if k != "tools"}
            response_text = await call_with_tool_prompt(
                route.litellm_model, norm_msgs, None, emul_extra
            )
            tool_calls = parse_tool_calls(response_text)
            if route.cot_engaged:
                response_text = strip_thinking(response_text)
            # v3.8.3 (#263) — emit telemetry with a synthetic OpenAI-shape
            # response body so the meta extractor walks the same path it
            # walks for native callers on /v1/chat/completions.
            import json as _json
            _emul_resp_body = {
                "choices": [{
                    "message": {
                        "tool_calls": [
                            {"function": {"name": tc.get("name", ""),
                                          "arguments": _json.dumps(tc.get("input", {}))}}
                            for tc in tool_calls
                        ],
                    },
                }],
            } if tool_calls else {"choices": [{"message": {"tool_calls": []}}]}
            await record_outcome(
                db, route.provider.id, route.litellm_model,
                endpoint="completions", success=True,
                t0=time.monotonic(), key_record_id=key_record.id,
                response_body=_emul_resp_body,
                tool_call_format="emulated",
            )
            if not allow_parallel and len(tool_calls) > 1:
                tool_calls = tool_calls[:1]
            if tool_calls:
                resp_headers["X-Tool-Calls-Emitted"] = str(len(tool_calls))
            if stream:
                if len(tool_calls) >= 2:
                    gen = openai_tools_sse(tool_calls)
                elif len(tool_calls) == 1:
                    gen = openai_tool_sse(tool_calls[0]["name"], tool_calls[0]["input"])
                else:
                    gen = openai_text_sse(response_text)
                return StreamingResponse(gen, media_type="text/event-stream", headers=resp_headers)
            else:
                if len(tool_calls) >= 2:
                    content = openai_tools_response(tool_calls, route.litellm_model)
                elif len(tool_calls) == 1:
                    content = openai_tool_response(tool_calls[0]["name"], tool_calls[0]["input"], route.litellm_model)
                else:
                    content = openai_text_response(response_text, route.litellm_model)
                # v3.6.1 — X-Quality-Hint for tool-emulation path
                from app.api._quality_hint import merge_into_headers
                merge_into_headers(resp_headers, content, endpoint="completions")
                return JSONResponse(content=content, headers=resp_headers)

        # CoT-E engagement.
        # v3.5.x R2 (2026-05-09): orchestration extracted to
        # _request_pipeline.maybe_engage_cot. The OpenAI flow uses
        # _stream_cot_openai with the standard arg list — no extras.
        from app.api._request_pipeline import maybe_engage_cot
        cot_resp = await maybe_engage_cot(
            route=route, stream=stream, db=db, key_record=key_record,
            hint=hint, body=body, messages_list=messages_list, extra=extra,
            x_cot_iterations=x_cot_iterations, x_cot_verify=x_cot_verify,
            x_cot_samples=x_cot_samples, x_cot_mode=x_cot_mode,
            x_session_id=x_session_id,
            resp_headers=resp_headers,
            stream_cot_fn=_stream_cot_openai,
        )
        if cot_resp is not None:
            return cot_resp

        if stream:
            lmrh_hedge = hint.get("hedge").value if (hint and hint.get("hedge")) else None
            wants_hedge = (
                _cfg_settings.hedge_enabled
                and should_hedge_header(x_hedge, lmrh_hedge)
            )
            wait_ms = wait_budget_ms(route.provider.id) if wants_hedge else None

            if wait_ms is not None and await try_acquire_hedge():
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
                        return _stream_openai(
                            route.litellm_model, messages_list, extra, route.provider.id,
                            db, key_record.id, time.monotonic(), budget_total,
                            cache_decision=cache_decision,
                            compliance_disclosure=_compliance_disclosure,
                            accept_compliance_events=_compliance_wants_sse_prelude,
                        )

                    def _backup():
                        b_extra = {**backup_route.litellm_kwargs}
                        if tools: b_extra["tools"] = tools
                        if body.get("max_tokens"): b_extra["max_tokens"] = body["max_tokens"]
                        if body.get("temperature") is not None: b_extra["temperature"] = body["temperature"]
                        if backup_route.native_thinking_params:
                            b_extra.update(backup_route.native_thinking_params)
                            clamp_thinking_budget(b_extra)
                        return _stream_openai(
                            backup_route.litellm_model, messages_list, b_extra,
                            backup_route.provider.id,
                            db, key_record.id, time.monotonic(), budget_total,
                            cache_decision=None,
                            compliance_disclosure=_compliance_disclosure,
                            accept_compliance_events=_compliance_wants_sse_prelude,
                        )

                    racer, winner = await race_streams(_primary, _backup, wait_ms)
                    observe_hedge_win(winner)
                    resp_headers["X-Hedged-Winner"] = winner
                    # v3.10.16 BUG-001 — pre-flight the hedged stream so a
                    # pre-stream upstream failure on the winning branch
                    # surfaces as a real HTTP status, not a 200 + SSE
                    # error frame (parity with the non-hedged path).
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

            # v3.10.13 BUG-001 — pre-flight so a pre-stream upstream
            # failure surfaces as a real HTTP status, not a 200 + a
            # terminal SSE error frame. (Mirrors the /v1/messages path.)
            _gen = _stream_openai(
                route.litellm_model, messages_list, extra, route.provider.id,
                db, key_record.id, time.monotonic(), budget_total,
                cache_decision=cache_decision,
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

            async def _replay_openai_stream(_f=_first, _g=_gen):
                yield _f
                async for _c in _g:
                    yield _c

            return StreamingResponse(
                _replay_openai_stream(),
                media_type="text/event-stream",
                headers=resp_headers,
            )
        else:
            t0 = time.monotonic()

            # Wave 5 #24 — structured output repair loop for response_format
            if (_cfg_settings.structured_output_enabled and not has_tools):
                from app.cot.structured_output import extract_openai_schema, call_with_schema
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
                    resp_headers["X-Structured-Output-Status"] = "valid" if parsed is not None else "invalid"
                    final_text = json.dumps(parsed) if parsed is not None else raw_text
                    await record_outcome(
                        db, route.provider.id, route.litellm_model, endpoint="completions",
                        success=True, t0=t0, key_record_id=key_record.id, provider_name=route.provider.name
                    )
                    try:
                        await maybe_store(cache_decision, final_text)
                    except Exception:
                        pass
                    # Build an OpenAI-format response manually so the output
                    # is exactly the validated JSON (no wrapper fences).
                    return JSONResponse(
                        content={
                            "id": f"chatcmpl-struct-{int(time.monotonic()*1000)}",
                            "object": "chat.completion",
                            "model": route.litellm_model,
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": final_text},
                                "finish_reason": "stop",
                            }],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
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
                    if "reasoning_effort" in r.native_thinking_params and body.get("reasoning_effort"):
                        local_extra["reasoning_effort"] = body["reasoning_effort"]
                    clamp_thinking_budget(local_extra)
                return await acompletion_with_retry(
                    model=r.litellm_model, messages=messages_list,
                    stream=False, **local_extra,
                )

            if _cfg_settings.fallback_enabled:
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
                    route = final_route
            else:
                result = await acompletion_with_retry(
                    model=route.litellm_model, messages=messages_list,
                    stream=False, **extra,
                )
            in_tok = getattr(result.usage, "prompt_tokens", 0)
            out_tok = getattr(result.usage, "completion_tokens", 0)
            try:
                answer_text = result.choices[0].message.content or ""
                await maybe_store(cache_decision, answer_text)
            except Exception:
                pass
            await record_outcome(
                db, route.provider.id, route.litellm_model,
                endpoint="completions", success=True,
                in_tok=in_tok, out_tok=out_tok, t0=t0,
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
                looks_like_empty_success_failure,
                empty_success_failure_message,
            )
            if looks_like_empty_success_failure(response_dict=_result_body):
                msg = empty_success_failure_message(_result_body)
                logger.warning(
                    "completions.empty_success_blocked provider=%s err=%s",
                    route.provider.name, msg,
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
            return JSONResponse(content=_result_body, headers=resp_headers)

    except Exception as e:
        err_str = str(e)
        await record_outcome(
            db, route.provider.id, route.litellm_model,
            endpoint="completions", success=False,
            key_record_id=key_record.id, error_str=err_str,
            provider_name=route.provider.name,
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
            request, db, route, key_record, _orig_request_model, status_code,
        )
        raise HTTPException(
            status_code,
            f"Upstream provider error ({cls}): {clean}",
            headers=error_headers or None,
        )


