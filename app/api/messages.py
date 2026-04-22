"""
/v1/messages — Anthropic-format endpoint (same path as v1).
Handles both streaming and non-streaming responses.
"""
import json
import logging
import time
from typing import Optional, AsyncIterator

import litellm
from fastapi import APIRouter, Request, Depends, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.auth.keys import verify_api_key, ApiKeyRecord
from app.routing.router import select_provider
from app.routing.lmrh import parse_hint
from app.cot.pipeline import run_cot_pipeline
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
)
from app.monitoring.helpers import record_outcome
from app.api.image_utils import has_images_anthropic, strip_images_anthropic

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/messages")
async def messages(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    llm_hint: Optional[str] = Header(None, alias="llm-hint"),
    x_session_id: Optional[str] = Header(None, alias="x-session-id"),
    x_cot_iterations: Optional[str] = Header(None, alias="x-cot-iterations"),
    x_cot_verify: Optional[str] = Header(None, alias="x-cot-verify"),
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

    route = await select_provider(
        db, hint, has_tools=has_tools, has_images=has_images, key_type=key_record.key_type
    )

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

    if route.vision_stripped:
        messages_list = strip_images_anthropic(messages_list)

    resp_headers = {
        "X-Provider": route.provider.name,
        "LLM-Capability": route.capability_header,
    }

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
            cot_max = None
            if x_cot_iterations is not None:
                try:
                    cot_max = max(0, int(x_cot_iterations))
                except ValueError:
                    pass
            force_verify: bool | None = None
            if x_cot_verify is not None:
                force_verify = x_cot_verify.lower() in ("1", "true", "yes")
            return StreamingResponse(
                _stream_cot_anthropic(
                    route.litellm_model, messages_list, x_session_id, extra,
                    cot_max, route.provider.id, db, key_record.id, force_verify,
                ),
                media_type="text/event-stream",
                headers=resp_headers,
            )

        if stream:
            return StreamingResponse(
                _stream_anthropic(route.litellm_model, messages_list, extra, route.provider.id,
                                  db, key_record.id, time.monotonic()),
                media_type="text/event-stream",
                headers=resp_headers,
            )
        else:
            t0 = time.monotonic()
            result = await litellm.acompletion(
                model=route.litellm_model,
                messages=messages_list,
                stream=False,
                **extra,
            )
            in_tok = getattr(result.usage, "prompt_tokens", 0)
            out_tok = getattr(result.usage, "completion_tokens", 0)
            await record_outcome(db, route.provider.id, route.litellm_model, success=True,
                                 in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record.id)
            return JSONResponse(
                content=_to_anthropic_response(result),
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
                except (ValueError, KeyError):
                    pass
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id)
    except Exception as e:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"type": "error", "error": {"message": str(e)}}).encode() + b'\n\n')


async def _stream_anthropic(
    model: str, messages: list, extra: dict, provider_id: str,
    db: AsyncSession, key_record_id: str, t0: float,
) -> AsyncIterator[bytes]:
    try:
        response = await litellm.acompletion(model=model, messages=messages, stream=True, **extra)
        index = 0
        text_started = False
        tool_started = False
        finish_reason = "stop"
        input_tokens = 0
        output_tokens = 0
        streamed_chars = 0
        tool_id: str = ""
        tool_name: str = ""

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

            # Tool call streaming
            tool_calls = getattr(delta, "tool_calls", None) or []
            for tc_delta in tool_calls:
                fn = getattr(tc_delta, "function", None)
                if not fn:
                    continue
                if not tool_started:
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
                yield f'data: {{"type":"content_block_start","index":{index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                text_started = True
            if content:
                streamed_chars += len(content)
                escaped = json.dumps(content)[1:-1]
                yield f'data: {{"type":"content_block_delta","index":{index},"delta":{{"type":"text_delta","text":"{escaped}"}}}}\n\n'.encode()

        if text_started or tool_started:
            yield f'data: {{"type":"content_block_stop","index":{index}}}\n\n'.encode()

        # Use reported usage if available; fall back to char-based estimate
        if output_tokens == 0 and streamed_chars > 0:
            output_tokens = max(1, streamed_chars // 4)

        stop_reason = _FINISH_TO_STOP.get(finish_reason, "end_turn")
        yield (
            f'data: {{"type":"message_delta","delta":{{"stop_reason":"{stop_reason}",'
            f'"stop_sequence":null}},"usage":{{"output_tokens":{output_tokens}}}}}\n\n'
        ).encode()
        yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=input_tokens, out_tok=output_tokens, t0=t0, key_record_id=key_record_id)
    except Exception as e:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"type": "error", "error": {"message": str(e)}}).encode() + b'\n\n')


_FINISH_TO_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}


def _to_anthropic_response(litellm_response) -> dict:
    """Convert litellm response to Anthropic messages API format."""
    import secrets as _secrets
    choice = litellm_response.choices[0]
    finish = choice.finish_reason or "stop"
    content: list = []
    if choice.message.content:
        content.append({"type": "text", "text": choice.message.content})
    tool_calls = getattr(choice.message, "tool_calls", None) or []
    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        if not fn:
            continue
        try:
            tool_input = json.loads(getattr(fn, "arguments", "{}") or "{}")
        except (ValueError, TypeError):
            tool_input = {}
        content.append({
            "type": "tool_use",
            "id": getattr(tc, "id", None) or f"toolu_{_secrets.token_hex(8)}",
            "name": getattr(fn, "name", "") or "",
            "input": tool_input,
        })
    if not content:
        content = [{"type": "text", "text": ""}]
    return {
        "id": litellm_response.id or "msg_proxy",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": litellm_response.model or "unknown",
        "stop_reason": _FINISH_TO_STOP.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": getattr(litellm_response.usage, "prompt_tokens", 0),
            "output_tokens": getattr(litellm_response.usage, "completion_tokens", 0),
        },
    }


