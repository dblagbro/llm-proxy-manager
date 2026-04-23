"""
/v1/messages — Anthropic-format endpoint (same path as v1).
Handles both streaming and non-streaming responses.
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
from app.auth.keys import verify_api_key, ApiKeyRecord
from app.routing.router import select_provider
from app.routing.lmrh import parse_hint
from app.cot.pipeline import run_cot_pipeline, parse_cot_request_headers
from app.cot.tool_emulation import (
    build_anthropic_tool_prompt,
    normalize_anthropic_messages,
    parse_tool_call,
    call_with_tool_prompt,
)
from app.cot.sse import (
    anthropic_tool_sse,
    anthropic_text_sse,
    anthropic_tool_response,
    anthropic_text_response,
    FINISH_TO_STOP,
    to_anthropic_response,
)
from app.monitoring.helpers import record_outcome
from app.api.image_utils import has_images_anthropic, strip_images_anthropic
from app.routing.aliases import resolve_alias
from app.api.webhook import post_webhook
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
    x_webhook_url: Optional[str] = Header(None, alias="x-webhook-url"),
    anthropic_beta: Optional[str] = Header(None, alias="anthropic-beta"),
    x_cache: Optional[str] = Header(None, alias="x-cache"),
    x_cache_ttl: Optional[str] = Header(None, alias="x-cache-ttl"),
    x_hedge: Optional[str] = Header(None, alias="x-hedge"),
):
    key_record = await verify_api_key(db, x_api_key)

    body = await request.json()
    messages_list = body.get("messages", [])
    stream = body.get("stream", False)
    max_tokens = body.get("max_tokens", 1024)
    system = body.get("system")
    thinking = body.get("thinking")
    tools = body.get("tools")

    hint = parse_hint(llm_hint)
    has_tools = bool(tools)
    has_images = has_images_anthropic(messages_list)

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

    if route.vision_stripped:
        messages_list = strip_images_anthropic(messages_list)

    resp_headers = {
        "X-Provider": route.provider.name,
        "LLM-Capability": route.capability_header,
        "X-Resolved-Model": route.litellm_model,
        "X-Token-Budget-Remaining": str(max_tokens),
    }
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
            from app.cot.sse import anthropic_text_sse, anthropic_text_response
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
            tool_system = build_anthropic_tool_prompt(tools or [])
            merged_system = tool_system + ("\n\n" + system if system else "")
            norm_msgs = normalize_anthropic_messages(messages_list)
            emul_extra = {k: v for k, v in extra.items() if k not in ("tools", "system")}
            response_text = await call_with_tool_prompt(
                route.litellm_model, norm_msgs, merged_system, emul_extra
            )
            await record_outcome(db, route.provider.id, route.litellm_model, success=True,
                                 t0=time.monotonic(), key_record_id=key_record.id)
            tool_call = parse_tool_call(response_text)
            if stream:
                gen = (
                    anthropic_tool_sse(tool_call["name"], tool_call["input"])
                    if tool_call else anthropic_text_sse(response_text)
                )
                return StreamingResponse(gen, media_type="text/event-stream", headers=resp_headers)
            else:
                content = (
                    anthropic_tool_response(tool_call["name"], tool_call["input"], route.litellm_model)
                    if tool_call else anthropic_text_response(response_text, route.litellm_model)
                )
                return JSONResponse(content=content, headers=resp_headers)

        if route.cot_engaged:
            if not stream:
                raise HTTPException(422, "CoT-E requires stream=true")
            cot_max, force_verify = parse_cot_request_headers(x_cot_iterations, x_cot_verify)
            return StreamingResponse(
                _stream_cot_anthropic(
                    route.litellm_model, messages_list, x_session_id, extra,
                    cot_max, route.provider.id, db, key_record.id, force_verify,
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
                                 cache_creation=cache_creation, cache_read=cache_read)
            # Store in semantic cache (fire-and-forget; won't affect response latency)
            try:
                answer_text = result.choices[0].message.content or ""
                await maybe_store(cache_decision, answer_text)
            except Exception:
                pass
            remaining = max(0, max_tokens - out_tok)
            resp_headers["X-Token-Budget-Remaining"] = str(remaining)
            return JSONResponse(
                content=to_anthropic_response(result),
                headers=resp_headers,
            )

    except Exception as e:
        err_str = str(e)
        await record_outcome(db, route.provider.id, route.litellm_model, success=False,
                             key_record_id=key_record.id, error_str=err_str)
        logger.error(f"Provider {route.provider.id} failed: {err_str}")
        raise HTTPException(502, f"Upstream provider error: {err_str}")


async def _stream_cot_anthropic(
    model: str,
    messages: list,
    session_id: str | None,
    extra: dict,
    max_iterations: int | None,
    provider_id: str,
    db: AsyncSession,
    key_record_id: str,
    force_verify: bool | None = None,
) -> AsyncIterator[bytes]:
    """Pass-through wrapper around run_cot_pipeline; records metrics after completion."""
    import json as _json
    in_tok = out_tok = 0
    cache_creation = cache_read = 0
    t0 = time.monotonic()
    try:
        async for chunk in run_cot_pipeline(model, messages, session_id, extra, max_iterations, force_verify):
            yield chunk
            # Extract token counts from the message_delta usage event
            line = chunk.decode(errors="ignore").strip()
            if line.startswith("data: "):
                try:
                    evt = _json.loads(line[6:])
                    if evt.get("type") == "message_delta":
                        usage = evt.get("usage", {})
                        in_tok = usage.get("input_tokens", in_tok)
                        out_tok = usage.get("output_tokens", out_tok)
                        cache_creation = usage.get("cache_creation_input_tokens", cache_creation) or cache_creation
                        cache_read = usage.get("cache_read_input_tokens", cache_read) or cache_read
                except (ValueError, KeyError):
                    pass
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id,
                             cache_creation=cache_creation, cache_read=cache_read)
    except Exception as e:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"type": "error", "error": {"message": str(e)}}).encode() + b'\n\n')
        yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'


async def _stream_anthropic(
    model: str, messages: list, extra: dict, provider_id: str,
    db: AsyncSession, key_record_id: str, t0: float, budget_total: int = 0,
    cache_decision=None,
) -> AsyncIterator[bytes]:
    try:
        response = await acompletion_with_retry(model=model, messages=messages, stream=True, **extra)
        index = 0
        text_started = False
        tool_started = False
        finish_reason = "stop"
        input_tokens = 0
        output_tokens = 0
        cache_creation = 0
        cache_read = 0
        streamed_chars = 0
        tool_id: str = ""
        tool_name: str = ""
        ttft_ms: float = 0.0
        full_text_buf: list[str] = []

        yield (
            f'data: {{"type":"message_start","message":{{"id":"msg_proxy","type":"message",'
            f'"role":"assistant","content":[],"model":"{model}",'
            f'"stop_reason":null,"stop_sequence":null,'
            f'"usage":{{"input_tokens":0,"output_tokens":0}}}}}}\n\n'
        ).encode()

        async for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            if hasattr(chunk, "usage") and chunk.usage:
                input_tokens = getattr(chunk.usage, "prompt_tokens", input_tokens)
                output_tokens = getattr(chunk.usage, "completion_tokens", output_tokens)
                from app.cot.sse import extract_cache_tokens
                c_create, c_read = extract_cache_tokens(chunk.usage)
                if c_create:
                    cache_creation = c_create
                if c_read:
                    cache_read = c_read

            # Tool call streaming
            tool_calls = getattr(delta, "tool_calls", None) or []
            for tc_delta in tool_calls:
                fn = getattr(tc_delta, "function", None)
                if not fn:
                    continue
                if not tool_started:
                    if not ttft_ms:
                        ttft_ms = (time.monotonic() - t0) * 1000
                    tool_id = getattr(tc_delta, "id", "") or f"toolu_{id(tc_delta)}"
                    tool_name = getattr(fn, "name", "") or ""
                    yield (
                        f'data: {{"type":"content_block_start","index":{index},'
                        f'"content_block":{{"type":"tool_use","id":"{tool_id}",'
                        f'"name":"{tool_name}","input":{{}}}}}}\n\n'
                    ).encode()
                    tool_started = True
                args_fragment = getattr(fn, "arguments", "") or ""
                if args_fragment:
                    escaped = json.dumps(args_fragment)[1:-1]
                    yield (
                        f'data: {{"type":"content_block_delta","index":{index},'
                        f'"delta":{{"type":"input_json_delta","partial_json":"{escaped}"}}}}\n\n'
                    ).encode()

            # Text streaming
            content = getattr(delta, "content", None) or ""
            if not text_started and content:
                if not ttft_ms:
                    ttft_ms = (time.monotonic() - t0) * 1000
                yield f'data: {{"type":"content_block_start","index":{index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                text_started = True
            if content:
                streamed_chars += len(content)
                full_text_buf.append(content)
                escaped = json.dumps(content)[1:-1]
                yield f'data: {{"type":"content_block_delta","index":{index},"delta":{{"type":"text_delta","text":"{escaped}"}}}}\n\n'.encode()

        if text_started or tool_started:
            yield f'data: {{"type":"content_block_stop","index":{index}}}\n\n'.encode()

        # Use reported usage if available; fall back to char-based estimate
        if output_tokens == 0 and streamed_chars > 0:
            output_tokens = max(1, streamed_chars // 4)

        stop_reason = FINISH_TO_STOP.get(finish_reason, "end_turn")
        usage_parts = [f'"output_tokens":{output_tokens}']
        if cache_creation:
            usage_parts.append(f'"cache_creation_input_tokens":{cache_creation}')
        if cache_read:
            usage_parts.append(f'"cache_read_input_tokens":{cache_read}')
        yield (
            f'data: {{"type":"message_delta","delta":{{"stop_reason":"{stop_reason}",'
            f'"stop_sequence":null}},"usage":{{{",".join(usage_parts)}}}}}\n\n'
        ).encode()
        if budget_total > 0:
            remaining = max(0, budget_total - output_tokens)
            yield (
                f'event: budget\ndata: {{"remaining":{remaining},'
                f'"used":{output_tokens},"total":{budget_total}}}\n\n'
            ).encode()
        yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=input_tokens, out_tok=output_tokens, t0=t0,
                             key_record_id=key_record_id, ttft_ms=ttft_ms,
                             cache_creation=cache_creation, cache_read=cache_read)
        if cache_decision is not None and cache_decision.eligible:
            try:
                await maybe_store(cache_decision, "".join(full_text_buf))
            except Exception:
                pass
    except Exception as e:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"type": "error", "error": {"message": str(e)}}).encode() + b'\n\n')
        yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'


async def _webhook_completion_anthropic(
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
        from app.cot.sse import extract_cache_tokens
        cache_creation, cache_read = extract_cache_tokens(result.usage)
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id,
                             cache_creation=cache_creation, cache_read=cache_read)
        await post_webhook(webhook_url, {
            "provider": provider_id,
            "model": model,
            "response": to_anthropic_response(result),
        })
    except Exception as exc:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=str(exc))
        await post_webhook(webhook_url, {"error": str(exc), "model": model})
