"""
/v1/messages — Anthropic-format endpoint (same path as v1).
Handles both streaming and non-streaming responses.
"""
import json
import logging
import time
from typing import Optional, AsyncIterator

import httpx
import litellm
from fastapi import APIRouter, BackgroundTasks, Request, Depends, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.auth.keys import verify_api_key, ApiKeyRecord
from app.routing.router import select_provider
from app.routing.lmrh import parse_hint
from app.cot.pipeline import run_cot_pipeline, parse_cot_request_headers
from app.cot.tool_emulation import (
    build_anthropic_tool_prompt,
    normalize_anthropic_messages,
    parse_tool_call,
    parse_tool_calls,
    call_with_tool_prompt,
)
from app.cot.sse import (
    anthropic_tool_sse,
    anthropic_tools_sse,
    anthropic_text_sse,
    anthropic_tool_response,
    anthropic_tools_response,
    anthropic_text_response,
    FINISH_TO_STOP,
    to_anthropic_response,
)
from app.monitoring.helpers import record_outcome
from app.api.image_utils import has_images_anthropic, strip_images_anthropic
from app.routing.aliases import resolve_alias
from app.api.webhook import post_webhook
from app.api._messages_streaming import (
    _stream_cot_anthropic, _stream_anthropic, _webhook_completion_anthropic,
)
from app.routing.retry import acompletion_with_retry
from app.observability.otel import llm_span
from app.cache.middleware import decide_cacheable, maybe_check, maybe_store
from app.routing.hedging import (
    should_hedge_header, wait_budget_ms, race_streams, try_acquire_hedge,
)
from app.config import settings
from app.observability.prometheus import (
    observe_hedge_attempt, observe_hedge_win, observe_hedge_bucket_reject,
)

logger = logging.getLogger(__name__)
router = APIRouter()


async def _select_excluding(db, hint, has_tools, has_images, key_type, excluded: set[str]):
    """v2.8.6: select_provider only accepts a single exclude_id. To walk
    through a chain of OAuth providers we need to call it repeatedly,
    excluding one id per pass and discarding any pick already in the
    tried set. Once we land on a never-tried provider, return its route."""
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
):
    key_record = await verify_api_key(db, x_api_key)

    body = await request.json()
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
    # v3.0.5: convert provider-selection RuntimeError into a clean 503 with
    # an actionable message. Hits when (a) all providers' CBs are open
    # (typical: Anthropic server-side OAuth token revocation triggers the
    # 24h auth-failure breaker on every claude-oauth provider on a node)
    # or (b) no providers configured at all. Same shape as the v3.0.4 fix
    # for /v1/chat/completions; previously bubbled to a 500 + ASGI trace.
    try:
        route = await select_provider(
            db, hint, has_tools=has_tools, has_images=has_images, key_type=key_record.key_type,
            pinned_provider_id=alias.provider_id if alias else None,
            model_override=alias.model_id if alias else None,
            sort_mode=parsed_slug.sort_mode,
        )
    except RuntimeError as e:
        msg = str(e)
        if "circuit breakers open" in msg:
            raise HTTPException(
                503,
                "All providers are currently unavailable (circuit breakers open). "
                "Most common cause: Anthropic server-side OAuth token revocation "
                "trips the 24h auth-failure breaker on every claude-oauth provider. "
                "Operator action: re-auth the affected provider(s) via the OAuth UI, "
                "or wait for the hold-down to expire.",
            )
        if "No providers configured" in msg:
            raise HTTPException(
                503,
                "No providers configured. Operator action: enable at least one "
                "provider via the Providers page or POST /api/providers.",
            )
        raise HTTPException(503, f"Provider selection failed: {msg}")
    if is_auto:
        # Substitute the resolved model into the body so claude-oauth /
        # litellm dispatch sees a real model name. Prefer the capability
        # profile's model_id (it's what we actually scored) — fall back to
        # provider.default_model if the profile is synthetic.
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
    # Budget visibility headers (soft-cap warning, remaining $ today/this hour)
    if key_record.budget_status is not None:
        from app.budget.tracker import warnings_for
        resp_headers.update(warnings_for(key_record.budget_status))

    # ── v2.7.0: claude-oauth dispatch ──────────────────────────────────────
    # Short-circuits the rest of the pipeline (no CoT, no tool emulation, no
    # fallback chains, no cascade) — Claude Pro Max subscriptions already run
    # through Claude Code's server-side routing, so we just forward the raw
    # /v1/messages body to platform.claude.com with the OAuth header bundle.
    # v2.8.6: when claude-oauth fails over, the next-priority provider may
    # ALSO be claude-oauth (e.g. Devin-VG → Devin-Gmail). The old single-shot
    # dispatch below would fall through to the litellm path and try to send
    # an OAuth token as an x-api-key header — wrong auth method, weird errors.
    # We now walk down the OAuth chain first; only after all OAuth options
    # are exhausted does the request fall into the regular litellm path.
    tried_oauth_ids: set[str] = set()
    while route.provider.provider_type == "claude-oauth":
        from app.api._messages_streaming import (
            _stream_claude_oauth, _complete_claude_oauth,
        )
        access_token = route.provider.api_key or ""
        t0 = time.monotonic()
        upstream_body = dict(body)
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
            )
        try:
            result = await _complete_claude_oauth(
                access_token, upstream_body,
                provider_id=oauth_provider_id, db=db,
                key_record_id=key_record.id, t0=t0,
                provider_name=route.provider.name,
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
            return JSONResponse(content=result, headers=resp_headers)
        # Defensive: should be unreachable — every branch above either returned,
        # raised, or continued. Break to avoid an accidental infinite loop.
        break

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
        system=system,
        tools=tools,
        x_cache_ttl_header=x_cache_ttl,
    )
    resp_headers["X-Cache-Status"] = "bypass" if not cache_decision.eligible else "miss"
    if cache_decision.eligible:
        cache_hit = await maybe_check(cache_decision, endpoint="messages")
        if cache_hit:
            resp_headers["X-Cache-Status"] = "hit"
            resp_headers["X-Cache-Similarity"] = f"{cache_hit.similarity:.3f}"
            # v2.8.6 — DO NOT re-import anthropic_text_sse / anthropic_text_response
            # locally here. They're imported at module scope; a function-local
            # ``from … import …`` makes Python flag the name as local, and if
            # this branch doesn't fire on a particular request, downstream
            # references at line 379/387 raise UnboundLocalError.
            if stream:
                return StreamingResponse(
                    anthropic_text_sse(cache_hit.response_text),
                    media_type="text/event-stream",
                    headers=resp_headers,
                )
            return JSONResponse(
                content=anthropic_text_response(cache_hit.response_text, route.litellm_model),
                headers=resp_headers,
            )

    # Webhook async: fire-and-forget completion, return 202 immediately
    if x_webhook_url:
        background_tasks.add_task(
            _webhook_completion_anthropic,
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
            # Wave 5 #23 — respect parallel_tool_calls from the inbound body
            # (Anthropic expresses this as tool_choice={disable_parallel_tool_use:true})
            tool_choice = body.get("tool_choice") or {}
            allow_parallel = True
            if isinstance(tool_choice, dict) and tool_choice.get("disable_parallel_tool_use"):
                allow_parallel = False
            tool_system = build_anthropic_tool_prompt(tools or [], allow_parallel=allow_parallel)
            merged_system = tool_system + ("\n\n" + system if system else "")
            norm_msgs = normalize_anthropic_messages(messages_list)
            emul_extra = {k: v for k, v in extra.items() if k not in ("tools", "system")}
            response_text = await call_with_tool_prompt(
                route.litellm_model, norm_msgs, merged_system, emul_extra
            )
            await record_outcome(db, route.provider.id, route.litellm_model, success=True,
                                 t0=time.monotonic(), key_record_id=key_record.id)
            tool_calls = parse_tool_calls(response_text)
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
                return JSONResponse(content=content, headers=resp_headers)

        if route.cot_engaged:
            if not stream:
                raise HTTPException(422, "CoT-E requires stream=true")
            cot_max, force_verify, samples = parse_cot_request_headers(
                x_cot_iterations, x_cot_verify, x_cot_samples, x_cot_mode
            )
            if samples > 1:
                resp_headers["X-Cot-Samples"] = str(samples)
            # Wave 2 #11 — resolve task-adaptive branch from LMRH hint
            from app.cot.task_adaptive import select_task_branch
            lmrh_task = hint.get("task").value if (hint and hint.get("task")) else None
            task_branch = select_task_branch(lmrh_task)
            if task_branch:
                resp_headers["X-Cot-Task-Branch"] = task_branch
            # Wave 2 #8 — pick a different provider for the critique pass
            critique_model: Optional[str] = None
            critique_kwargs: Optional[dict] = None
            if settings.cot_cross_provider_critique:
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
                    pass  # no alternate available; critique stays on primary
            return StreamingResponse(
                _stream_cot_anthropic(
                    route.litellm_model, messages_list, x_session_id, extra,
                    cot_max, route.provider.id, db, key_record.id, force_verify,
                    critique_model=critique_model, critique_kwargs=critique_kwargs,
                    samples=samples, task_branch=task_branch,
                ),
                media_type="text/event-stream",
                headers=resp_headers,
            )

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
                _stream_anthropic(route.litellm_model, messages_list, extra, route.provider.id,
                                  db, key_record.id, time.monotonic(), max_tokens,
                                  cache_decision=cache_decision),
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
            if do_cascade and not has_tools and not route.cot_engaged:
                try:
                    cheap_route = await select_provider(
                        db, hint, has_tools=False, has_images=has_images,
                        key_type=key_record.key_type, prefer_cheapest=True,
                        excluded_provider_types={"claude-oauth"},
                    )
                    # Grader: use the current top (route) if different from cheap
                    grader_route = route if route.provider.id != cheap_route.provider.id else None
                    if grader_route is None:
                        try:
                            grader_route = await select_provider(
                                db, hint, has_tools=False, has_images=has_images,
                                key_type=key_record.key_type,
                                exclude_provider_id=cheap_route.provider.id,
                                prefer_cheapest=True,
                        excluded_provider_types={"claude-oauth"},
                            )
                        except Exception:
                            grader_route = None

                    cheap_result = await _call_with_route(cheap_route)
                    cheap_answer = cheap_result.choices[0].message.content or ""

                    # Extract last user turn for grader context
                    _user_text = ""
                    for _m in reversed(messages_list):
                        if _m.get("role") == "user":
                            _c = _m.get("content", "")
                            if isinstance(_c, str):
                                _user_text = _c
                            elif isinstance(_c, list):
                                _user_text = "\n".join(
                                    b.get("text", "") for b in _c
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                            break

                    if grader_route is not None:
                        verdict = await grade_answer(
                            grader_route.litellm_model, grader_route.litellm_kwargs,
                            _user_text, cheap_answer,
                        )
                    else:
                        # No distinct grader available → accept cheap result
                        verdict_acc = True
                        verdict = type("V", (), {"acceptable": True, "reason": "no-grader-available"})()

                    if verdict.acceptable:
                        resp_headers["X-Cascade"] = "accepted"
                        resp_headers["X-Cascade-Grader"] = (
                            grader_route.provider.name if grader_route else "—"
                        )
                        resp_headers["X-Provider"] = cheap_route.provider.name
                        resp_headers["X-Resolved-Model"] = cheap_route.litellm_model
                        result = cheap_result
                        route = cheap_route
                        in_tok = getattr(result.usage, "prompt_tokens", 0)
                        out_tok = getattr(result.usage, "completion_tokens", 0)
                        from app.cot.sse import extract_cache_tokens
                        cache_creation, cache_read = extract_cache_tokens(result.usage)
                        await record_outcome(db, route.provider.id, route.litellm_model, success=True,
                                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record.id,
                                             cache_creation=cache_creation, cache_read=cache_read, provider_name=route.provider.name)
                        try:
                            await maybe_store(cache_decision, cheap_answer)
                        except Exception:
                            pass
                        remaining = max(0, max_tokens - out_tok)
                        resp_headers["X-Token-Budget-Remaining"] = str(remaining)
                        return JSONResponse(
                            content=to_anthropic_response(result),
                            headers=resp_headers,
                        )
                    else:
                        # Escalate to the original top-ranked route (already in `route`)
                        resp_headers["X-Cascade"] = "escalated"
                        resp_headers["X-Cascade-Reason"] = verdict.reason[:100]
                        resp_headers["X-Cascade-Grader"] = grader_route.provider.name
                        # Fall through to fallback path below with `route` (top)
                except Exception as cascade_exc:
                    logger.warning(f"Cascade failed, falling through to default: {cascade_exc}")
                    resp_headers["X-Cascade"] = f"error:{type(cascade_exc).__name__}"

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
            await record_outcome(db, route.provider.id, route.litellm_model, success=True,
                                 in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record.id,
                                 cache_creation=cache_creation, cache_read=cache_read, provider_name=route.provider.name)
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
            return JSONResponse(
                content=to_anthropic_response(result),
                headers=resp_headers,
            )

    except Exception as e:
        err_str = str(e)
        await record_outcome(db, route.provider.id, route.litellm_model, success=False,
                             key_record_id=key_record.id, error_str=err_str, provider_name=route.provider.name)
        logger.error(f"Provider {route.provider.id} failed: {err_str}")
        raise HTTPException(502, f"Upstream provider error: {err_str}")


