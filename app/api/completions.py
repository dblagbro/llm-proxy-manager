"""
/v1/chat/completions — OpenAI-format endpoint (same path as v1).
"""
import json
import logging
import time
from typing import Optional, AsyncIterator

import litellm
from fastapi import APIRouter, BackgroundTasks, Request, Depends, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.auth.keys import verify_api_key
from app.routing.router import select_provider
from app.routing.lmrh import parse_hint
from app.monitoring.helpers import record_outcome
from app.api.image_utils import has_images_openai, strip_images_openai
from app.routing.aliases import resolve_alias
from app.cot.pipeline import run_cot_pipeline, parse_cot_request_headers
from app.cot.tool_emulation import (
    build_openai_tool_prompt,
    normalize_openai_messages,
    parse_tool_call,
    parse_tool_calls,
    call_with_tool_prompt,
)
from app.cot.sse import (
    openai_tool_sse,
    openai_tools_sse,
    openai_text_sse,
    openai_tool_response,
    openai_tools_response,
    openai_text_response,
)
from app.api.webhook import post_webhook
from app.api._completions_streaming import (
    _stream_cot_openai, _stream_openai, _webhook_completion_openai,
)
from app.routing.retry import acompletion_with_retry
from app.observability.otel import llm_span
from app.cache.middleware import decide_cacheable, maybe_check, maybe_store
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
):
    # Accept Bearer token or x-api-key
    token = x_api_key
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    key_record = await verify_api_key(db, token)

    body = await request.json()
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

    # v2.8.0: parse :floor / :nitro / :exacto suffix + auto-routing alias.
    from app.routing.model_slug import parse_model_slug, is_auto_model
    parsed_slug = parse_model_slug(body.get("model"))
    if parsed_slug.sort_mode is not None:
        body = {**body, "model": parsed_slug.bare_model}

    is_auto = is_auto_model(parsed_slug.bare_model)
    alias = await resolve_alias(db, body.get("model")) if not is_auto else None
    # v2.8.11: /v1/chat/completions has no claude-oauth dispatch — those
    # providers require the dedicated handler in messages.py (OAuth Bearer +
    # CC beta flags + Anthropic body shape). Sending one through litellm here
    # leaks the OAuth token as an x-api-key and produces a confusing 401 or
    # "Connection error" upstream. Filter them out at route selection.
    # v3.0.4: convert the no-providers-available RuntimeError into a clean
    # 503 with an actionable message instead of letting it bubble to a
    # raw 500 + ASGI traceback. Hits when the only enabled providers are
    # claude-oauth (cutover window state).
    try:
        route = await select_provider(
            db, hint, has_tools=has_tools, has_images=has_images, key_type=key_record.key_type,
            pinned_provider_id=alias.provider_id if alias else None,
            model_override=alias.model_id if alias else None,
            sort_mode=parsed_slug.sort_mode,
            excluded_provider_types={"claude-oauth"},
        )
    except RuntimeError as e:
        msg = str(e)
        if "claude-oauth" in msg:
            raise HTTPException(
                503,
                "No OpenAI-compatible providers available — only claude-oauth "
                "providers are currently enabled. /v1/chat/completions cannot "
                "dispatch to claude-oauth (Anthropic-format only); use "
                "/v1/messages or enable an openai/anthropic/google provider.",
            )
        raise HTTPException(503, f"Provider selection failed: {msg}")
    if is_auto:
        resolved_model = route.profile.model_id or route.provider.default_model
        if not resolved_model:
            raise HTTPException(
                502,
                f"auto-routing chose {route.provider.name!r} but it has no default_model set",
            )
        body = {**body, "model": resolved_model}

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
    # Budget visibility headers (soft-cap warning, remaining $ today/this hour)
    if key_record.budget_status is not None:
        from app.budget.tracker import warnings_for
        resp_headers.update(warnings_for(key_record.budget_status))

    # Semantic cache — check before anything LLM-ish runs
    cache_decision = decide_cacheable(
        x_cache_header=x_cache,
        api_key_opt_in=bool(getattr(key_record, "semantic_cache_enabled", False)),
        key_type=key_record.key_type,
        cot_engaged=route.cot_engaged,
        tool_emulation=route.tool_emulation_engaged,
        has_tools=has_tools,
        webhook_url=x_webhook_url,
        temperature=body.get("temperature"),
        messages=messages_list,
        model=route.litellm_model,
        tenant_id=key_record.id,
        system=None,
        tools=tools,
        x_cache_ttl_header=x_cache_ttl,
    )
    resp_headers["X-Cache-Status"] = "bypass" if not cache_decision.eligible else "miss"
    if cache_decision.eligible:
        cache_hit = await maybe_check(cache_decision, endpoint="completions")
        if cache_hit:
            resp_headers["X-Cache-Status"] = "hit"
            resp_headers["X-Cache-Similarity"] = f"{cache_hit.similarity:.3f}"
            if stream:
                return StreamingResponse(
                    openai_text_sse(cache_hit.response_text),
                    media_type="text/event-stream",
                    headers=resp_headers,
                )
            return JSONResponse(
                content=openai_text_response(cache_hit.response_text, route.litellm_model),
                headers=resp_headers,
            )

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
            tool_prompt = build_openai_tool_prompt(tools or [], allow_parallel=allow_parallel)
            norm_msgs = normalize_openai_messages(messages_list)
            if norm_msgs and norm_msgs[0]["role"] == "system":
                norm_msgs[0]["content"] = tool_prompt + "\n\n" + norm_msgs[0]["content"]
            else:
                norm_msgs = [{"role": "system", "content": tool_prompt}] + norm_msgs
            emul_extra = {k: v for k, v in extra.items() if k != "tools"}
            response_text = await call_with_tool_prompt(
                route.litellm_model, norm_msgs, None, emul_extra
            )
            await record_outcome(db, route.provider.id, route.litellm_model, endpoint="completions", success=True,
                                 t0=time.monotonic(), key_record_id=key_record.id)
            tool_calls = parse_tool_calls(response_text)
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
                return JSONResponse(content=content, headers=resp_headers)

        if route.cot_engaged:
            if not stream:
                raise HTTPException(422, "CoT-E requires stream=true")
            cot_max, force_verify, samples = parse_cot_request_headers(
                x_cot_iterations, x_cot_verify, x_cot_samples, x_cot_mode
            )
            if samples > 1:
                resp_headers["X-Cot-Samples"] = str(samples)
            from app.cot.task_adaptive import select_task_branch
            lmrh_task = hint.get("task").value if (hint and hint.get("task")) else None
            task_branch = select_task_branch(lmrh_task)
            if task_branch:
                resp_headers["X-Cot-Task-Branch"] = task_branch
            # Wave 2 #8 — pick a different provider for critique
            critique_model: Optional[str] = None
            critique_kwargs: Optional[dict] = None
            if _cfg_settings.cot_cross_provider_critique:
                try:
                    critique_route = await select_provider(
                        db, hint, has_tools=False, has_images=False,
                        key_type=key_record.key_type,
                        exclude_provider_id=route.provider.id,
                        excluded_provider_types={"claude-oauth"},
                    )
                    critique_model = critique_route.litellm_model
                    critique_kwargs = critique_route.litellm_kwargs
                    resp_headers["X-Critique-Provider"] = critique_route.provider.name
                except Exception:
                    pass
            return StreamingResponse(
                _stream_cot_openai(
                    route.litellm_model, messages_list, x_session_id, extra,
                    cot_max, route.provider.id, db, key_record.id, force_verify,
                    critique_model=critique_model, critique_kwargs=critique_kwargs,
                    samples=samples, task_branch=task_branch,
                ),
                media_type="text/event-stream",
                headers=resp_headers,
            )

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
                        )

                    def _backup():
                        b_extra = {**backup_route.litellm_kwargs}
                        if tools: b_extra["tools"] = tools
                        if body.get("max_tokens"): b_extra["max_tokens"] = body["max_tokens"]
                        if body.get("temperature") is not None: b_extra["temperature"] = body["temperature"]
                        if backup_route.native_thinking_params:
                            b_extra.update(backup_route.native_thinking_params)
                        return _stream_openai(
                            backup_route.litellm_model, messages_list, b_extra,
                            backup_route.provider.id,
                            db, key_record.id, time.monotonic(), budget_total,
                            cache_decision=None,
                        )

                    racer, winner = await race_streams(_primary, _backup, wait_ms)
                    observe_hedge_win(winner)
                    resp_headers["X-Hedged-Winner"] = winner
                    return StreamingResponse(
                        racer, media_type="text/event-stream", headers=resp_headers,
                    )
            elif wait_ms is not None:
                observe_hedge_bucket_reject()

            return StreamingResponse(
                _stream_openai(route.litellm_model, messages_list, extra, route.provider.id,
                               db, key_record.id, time.monotonic(), budget_total,
                               cache_decision=cache_decision),
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
            await record_outcome(db, route.provider.id, route.litellm_model, endpoint="completions", success=True,
                                 in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record.id, provider_name=route.provider.name)
            if budget_total:
                resp_headers["X-Token-Budget-Remaining"] = str(max(0, budget_total - out_tok))
            return JSONResponse(content=result.model_dump(), headers=resp_headers)

    except Exception as e:
        err_str = str(e)
        await record_outcome(db, route.provider.id, route.litellm_model, endpoint="completions", success=False,
                             key_record_id=key_record.id, error_str=err_str, provider_name=route.provider.name)
        raise HTTPException(502, f"Upstream provider error: {err_str}")


