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

    hint = parse_hint(llm_hint)
    has_tools = bool(tools)
    has_images = has_images_openai(messages_list)

    # Wave 3 #15 — auto-classify task= if not supplied and feature enabled
    auto_task: Optional[str] = None
    if _cfg_settings.task_auto_detect_enabled and (hint is None or not hint.get("task")):
        from app.routing.classifier import classify
        from app.routing.lmrh import LMRHHint, HintDimension
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
        cls = await classify(
            _user_text[:800],
            _cfg_settings.semantic_cache_embedding_model,
            _cfg_settings.semantic_cache_embedding_dims,
        )
        if cls:
            auto_task, _conf = cls
            if hint is None:
                hint = LMRHHint(raw=f"task={auto_task}")
            hint.dimensions.append(HintDimension("task", auto_task))

    alias = await resolve_alias(db, body.get("model"))
    route = await select_provider(
        db, hint, has_tools=has_tools, has_images=has_images, key_type=key_record.key_type,
        pinned_provider_id=alias.provider_id if alias else None,
        model_override=alias.model_id if alias else None,
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

    # Wave 5 #26 — long-context map-reduce / truncate / error
    from app.api.long_context import (
        needs_compression, resolve_strategy, truncate_to_window, mapreduce_compress,
    )
    context_strategy_applied: Optional[str] = None
    if needs_compression(messages_list, route.profile.context_length):
        strategy = resolve_strategy(x_context_strategy)
        if strategy == "error":
            raise HTTPException(
                413,
                f"Context window exceeded. Max: {route.profile.context_length}",
            )
        if strategy == "mapreduce":
            _user_q = ""
            for _m in reversed(messages_list):
                if _m.get("role") == "user":
                    _c = _m.get("content", "")
                    if isinstance(_c, str):
                        _user_q = _c
                    elif isinstance(_c, list):
                        _user_q = "\n".join(
                            b.get("text", "") for b in _c
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    break
            messages_list, chunks, _ = await mapreduce_compress(
                messages_list, model=route.litellm_model, extra=extra,
                context_length=route.profile.context_length,
                user_question=_user_q,
            )
            context_strategy_applied = f"mapreduce:{chunks}chunks"
        else:
            messages_list, dropped = truncate_to_window(
                messages_list, route.profile.context_length,
            )
            context_strategy_applied = f"truncate:{dropped}dropped"

    budget_total = body.get("max_tokens", 0) or 0
    resp_headers = {
        "X-Provider": route.provider.name,
        "X-Resolved-Provider": route.provider.provider_type,  # Wave 5 #28: honest disclosure
        "LLM-Capability": route.capability_header,
        "X-Resolved-Model": route.litellm_model,
    }
    # Wave 5 #28 — advertise emulation level
    _emul_level = "minimal"
    if route.tool_emulation_engaged or route.vision_stripped:
        _emul_level = "standard"
    if route.cot_engaged:
        _emul_level = "enhanced"
    resp_headers["X-Emulation-Level"] = _emul_level
    if auto_task:
        resp_headers["X-Task-Auto-Detected"] = auto_task
    if vision_routed_count:
        resp_headers["X-Vision-Routed"] = str(vision_routed_count)
    if context_strategy_applied:
        resp_headers["X-Context-Strategy-Applied"] = context_strategy_applied
    if hint is not None:
        from app.routing.lmrh import build_hint_set_header
        hint_set = build_hint_set_header(hint, route.unmet_hints)
        if hint_set:
            resp_headers["LLM-Hint-Set"] = hint_set
    if budget_total:
        resp_headers["X-Token-Budget-Remaining"] = str(budget_total)
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
                        success=True, t0=t0, key_record_id=key_record.id,
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
                                 in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record.id)
            if budget_total:
                resp_headers["X-Token-Budget-Remaining"] = str(max(0, budget_total - out_tok))
            return JSONResponse(content=result.model_dump(), headers=resp_headers)

    except Exception as e:
        err_str = str(e)
        await record_outcome(db, route.provider.id, route.litellm_model, endpoint="completions", success=False,
                             key_record_id=key_record.id, error_str=err_str)
        raise HTTPException(502, f"Upstream provider error: {err_str}")


async def _stream_cot_openai(
    model: str,
    messages: list,
    session_id: str | None,
    extra: dict,
    max_iterations: int | None,
    provider_id: str,
    db: AsyncSession,
    key_record_id: str,
    force_verify: bool | None = None,
    critique_model: str | None = None,
    critique_kwargs: dict | None = None,
    samples: int = 1,
    task_branch: str | None = None,
) -> AsyncIterator[bytes]:
    """
    Run the CoT-E pipeline and re-emit as OpenAI-format SSE chunks.
    Thinking blocks are collected and prepended as <thinking>…</thinking>
    text so callers can strip or display them.
    """
    thinking_buf: list[str] = []
    text_buf: list[str] = []
    in_thinking = False
    in_text = False
    in_tok = out_tok = 0
    t0 = time.monotonic()

    try:
        async for raw in run_cot_pipeline(
            model, messages, session_id, extra, max_iterations, force_verify,
            critique_model=critique_model, critique_kwargs=critique_kwargs,
            samples=samples, task_branch=task_branch,
        ):
            line = raw.decode(errors="ignore").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload in ("[DONE]", ""):
                continue
            try:
                evt = json.loads(payload)
            except ValueError:
                continue

            t = evt.get("type", "")
            if t == "content_block_start":
                block_type = evt.get("content_block", {}).get("type", "")
                in_thinking = block_type == "thinking"
                in_text = block_type == "text"
            elif t == "content_block_delta":
                delta = evt.get("delta", {})
                if in_thinking:
                    thinking_buf.append(delta.get("thinking", ""))
                elif in_text:
                    text_buf.append(delta.get("text", ""))
            elif t == "content_block_stop":
                in_thinking = False
                in_text = False
            elif t == "message_delta":
                usage = evt.get("usage", {})
                in_tok = usage.get("input_tokens", in_tok)
                out_tok = usage.get("output_tokens", out_tok)

        thinking_text = "".join(thinking_buf).strip()
        answer_text = "".join(text_buf).strip()
        full_text = (
            f"<thinking>\n{thinking_text}\n</thinking>\n\n{answer_text}"
            if thinking_text else answer_text
        )

        msg_id = "chatcmpl-cot"
        yield (
            f'data: {{"id":"{msg_id}","object":"chat.completion.chunk",'
            f'"choices":[{{"index":0,"delta":{{"role":"assistant"}},"finish_reason":null}}]}}\n\n'
        ).encode()
        chunk_size = 50
        for i in range(0, len(full_text), chunk_size):
            piece = json.dumps(full_text[i:i + chunk_size])[1:-1]
            yield (
                f'data: {{"id":"{msg_id}","object":"chat.completion.chunk",'
                f'"choices":[{{"index":0,"delta":{{"content":"{piece}"}},"finish_reason":null}}]}}\n\n'
            ).encode()
        yield (
            f'data: {{"id":"{msg_id}","object":"chat.completion.chunk",'
            f'"choices":[{{"index":0,"delta":{{}},"finish_reason":"stop"}}]}}\n\n'
        ).encode()
        yield b"data: [DONE]\n\n"

        await record_outcome(db, provider_id, model, endpoint="completions", success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id)

    except Exception as e:
        await record_outcome(db, provider_id, model, endpoint="completions", success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"error": str(e)}).encode() + b'\n\n')
        yield b'data: [DONE]\n\n'


async def _stream_openai(
    model: str, messages: list, extra: dict, provider_id: str,
    db: AsyncSession, key_record_id: str, t0: float, budget_total: int = 0,
    cache_decision=None,
) -> AsyncIterator[bytes]:
    in_tok = out_tok = 0
    ttft_ms: float = 0.0
    first_chunk = True
    full_text_buf: list[str] = []
    try:
        response = await acompletion_with_retry(model=model, messages=messages, stream=True, **extra)
        async for chunk in response:
            if first_chunk:
                ttft_ms = (time.monotonic() - t0) * 1000
                first_chunk = False
            if hasattr(chunk, "usage") and chunk.usage:
                in_tok = getattr(chunk.usage, "prompt_tokens", in_tok)
                out_tok = getattr(chunk.usage, "completion_tokens", out_tok)
            # Buffer text content for potential cache store
            try:
                delta = chunk.choices[0].delta if chunk.choices else None
                text = getattr(delta, "content", None) if delta else None
                if text:
                    full_text_buf.append(text)
            except Exception:
                pass
            yield f"data: {chunk.model_dump_json()}\n\n".encode()
        if budget_total > 0:
            remaining = max(0, budget_total - out_tok)
            yield (
                f'event: budget\ndata: {{"remaining":{remaining},'
                f'"used":{out_tok},"total":{budget_total}}}\n\n'
            ).encode()
        yield b"data: [DONE]\n\n"
        await record_outcome(db, provider_id, model, endpoint="completions", success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0,
                             key_record_id=key_record_id, ttft_ms=ttft_ms)
        if cache_decision is not None and cache_decision.eligible:
            try:
                await maybe_store(cache_decision, "".join(full_text_buf))
            except Exception:
                pass
    except Exception as e:
        await record_outcome(db, provider_id, model, endpoint="completions", success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"error": str(e)}).encode() + b'\n\n')
        yield b'data: [DONE]\n\n'


async def _webhook_completion_openai(
    webhook_url: str,
    model: str,
    messages: list,
    extra: dict,
    provider_id: str,
    db: AsyncSession,
    key_record_id: str,
) -> None:
    """Run a non-streaming completion and POST the result to webhook_url."""
    t0 = time.monotonic()
    try:
        result = await acompletion_with_retry(model=model, messages=messages, stream=False, **extra)
        in_tok = getattr(result.usage, "prompt_tokens", 0)
        out_tok = getattr(result.usage, "completion_tokens", 0)
        await record_outcome(db, provider_id, model, endpoint="completions", success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id)
        await post_webhook(webhook_url, {
            "provider": provider_id,
            "model": model,
            "response": result.model_dump(),
        })
    except Exception as exc:
        await record_outcome(db, provider_id, model, endpoint="completions", success=False,
                             key_record_id=key_record_id, error_str=str(exc))
        await post_webhook(webhook_url, {"error": str(exc), "model": model})
