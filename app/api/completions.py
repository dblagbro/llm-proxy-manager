"""
/v1/chat/completions — OpenAI-format endpoint (same path as v1).
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
from app.auth.keys import verify_api_key
from app.routing.router import select_provider
from app.routing.lmrh import parse_hint
from app.monitoring.helpers import record_outcome
from app.api.image_utils import has_images_openai, strip_images_openai
from app.cot.pipeline import run_cot_pipeline
from app.cot.tool_emulation import (
    build_openai_tool_prompt,
    normalize_openai_messages,
    parse_tool_call,
    call_with_tool_prompt,
    openai_tool_sse,
    openai_text_sse,
    openai_tool_response,
    openai_text_response,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    llm_hint: Optional[str] = Header(None, alias="llm-hint"),
    x_session_id: Optional[str] = Header(None, alias="x-session-id"),
    x_cot_iterations: Optional[str] = Header(None, alias="x-cot-iterations"),
    x_cot_verify: Optional[str] = Header(None, alias="x-cot-verify"),
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

    route = await select_provider(db, hint, has_tools=has_tools, has_images=has_images, key_type=key_record.key_type)

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

    if route.vision_stripped:
        messages_list = strip_images_openai(messages_list)

    resp_headers = {
        "X-Provider": route.provider.name,
        "LLM-Capability": route.capability_header,
    }

    try:
        if route.tool_emulation_engaged:
            # Inject tool schema as system prompt; normalize tool_use/result message history
            tool_prompt = build_openai_tool_prompt(tools or [])
            norm_msgs = normalize_openai_messages(messages_list)
            # Prepend/merge tool system prompt
            if norm_msgs and norm_msgs[0]["role"] == "system":
                norm_msgs[0]["content"] = tool_prompt + "\n\n" + norm_msgs[0]["content"]
            else:
                norm_msgs = [{"role": "system", "content": tool_prompt}] + norm_msgs
            emul_extra = {k: v for k, v in extra.items() if k != "tools"}
            response_text = await call_with_tool_prompt(
                route.litellm_model, norm_msgs, None, emul_extra
            )
            await record_outcome(db, route.provider.id, route.litellm_model, success=True,
                                 t0=time.monotonic(), key_record_id=key_record.id)
            tool_call = parse_tool_call(response_text)
            if stream:
                gen = (
                    openai_tool_sse(tool_call["name"], tool_call["input"])
                    if tool_call else openai_text_sse(response_text)
                )
                return StreamingResponse(gen, media_type="text/event-stream", headers=resp_headers)
            else:
                content = (
                    openai_tool_response(tool_call["name"], tool_call["input"], route.litellm_model)
                    if tool_call else openai_text_response(response_text, route.litellm_model)
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
                _stream_cot_openai(
                    route.litellm_model, messages_list, x_session_id, extra,
                    cot_max, route.provider.id, db, key_record.id, force_verify,
                ),
                media_type="text/event-stream",
                headers=resp_headers,
            )

        if stream:
            return StreamingResponse(
                _stream_openai(route.litellm_model, messages_list, extra, route.provider.id,
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
            return JSONResponse(content=result.model_dump(), headers=resp_headers)

    except Exception as e:
        err_str = str(e)
        await record_outcome(db, route.provider.id, route.litellm_model, success=False,
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
        async for raw in run_cot_pipeline(model, messages, session_id, extra, max_iterations, force_verify):
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

        await record_outcome(db, provider_id, model, success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id)

    except Exception as e:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"error": str(e)}).encode() + b'\n\n')


async def _stream_openai(
    model: str, messages: list, extra: dict, provider_id: str,
    db: AsyncSession, key_record_id: str, t0: float,
) -> AsyncIterator[bytes]:
    in_tok = out_tok = 0
    try:
        response = await litellm.acompletion(model=model, messages=messages, stream=True, **extra)
        async for chunk in response:
            if hasattr(chunk, "usage") and chunk.usage:
                in_tok = getattr(chunk.usage, "prompt_tokens", in_tok)
                out_tok = getattr(chunk.usage, "completion_tokens", out_tok)
            yield f"data: {chunk.model_dump_json()}\n\n".encode()
        yield b"data: [DONE]\n\n"
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id)
    except Exception as e:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"error": str(e)}).encode() + b'\n\n')
