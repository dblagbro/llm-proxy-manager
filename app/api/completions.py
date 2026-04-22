"""
/v1/chat/completions — OpenAI-format endpoint (same path as v1).
"""
import json
import logging
from typing import Optional, AsyncIterator

import litellm
from fastapi import APIRouter, Request, Depends, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.auth.keys import verify_api_key
from app.routing.router import select_provider
from app.routing.lmrh import parse_hint
from app.routing.circuit_breaker import record_success, record_failure, is_billing_error
from app.cot.pipeline import run_cot_pipeline

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

    route = await select_provider(db, hint, has_tools=has_tools, key_type=key_record.key_type)

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

    resp_headers = {
        "X-Provider": route.provider.name,
        "LLM-Capability": route.capability_header,
    }

    try:
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
                    cot_max, route.provider.id, force_verify,
                ),
                media_type="text/event-stream",
                headers=resp_headers,
            )

        if stream:
            return StreamingResponse(
                _stream_openai(route.litellm_model, messages_list, extra, route.provider.id),
                media_type="text/event-stream",
                headers=resp_headers,
            )
        else:
            result = await litellm.acompletion(
                model=route.litellm_model,
                messages=messages_list,
                stream=False,
                **extra,
            )
            await record_success(route.provider.id)
            return JSONResponse(content=result.model_dump(), headers=resp_headers)

    except Exception as e:
        err_str = str(e)
        await record_failure(route.provider.id, billing_error=is_billing_error(err_str))
        raise HTTPException(502, f"Upstream provider error: {err_str}")


async def _stream_cot_openai(
    model: str,
    messages: list,
    session_id: str | None,
    extra: dict,
    max_iterations: int | None,
    provider_id: str,
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

        thinking_text = "".join(thinking_buf).strip()
        answer_text = "".join(text_buf).strip()
        full_text = (
            f"<thinking>\n{thinking_text}\n</thinking>\n\n{answer_text}"
            if thinking_text else answer_text
        )

        msg_id = "chatcmpl-cot"
        # Role delta
        yield (
            f'data: {{"id":"{msg_id}","object":"chat.completion.chunk",'
            f'"choices":[{{"index":0,"delta":{{"role":"assistant"}},"finish_reason":null}}]}}\n\n'
        ).encode()
        # Content in chunks
        chunk_size = 50
        for i in range(0, len(full_text), chunk_size):
            piece = json.dumps(full_text[i:i + chunk_size])[1:-1]
            yield (
                f'data: {{"id":"{msg_id}","object":"chat.completion.chunk",'
                f'"choices":[{{"index":0,"delta":{{"content":"{piece}"}},"finish_reason":null}}]}}\n\n'
            ).encode()
        # Stop chunk
        yield (
            f'data: {{"id":"{msg_id}","object":"chat.completion.chunk",'
            f'"choices":[{{"index":0,"delta":{{}},"finish_reason":"stop"}}]}}\n\n'
        ).encode()
        yield b"data: [DONE]\n\n"
        await record_success(provider_id)

    except Exception as e:
        await record_failure(provider_id, billing_error=is_billing_error(str(e)))
        yield f'data: {{"error": "{str(e)}"}}\n\n'.encode()


async def _stream_openai(
    model: str, messages: list, extra: dict, provider_id: str
) -> AsyncIterator[bytes]:
    try:
        response = await litellm.acompletion(model=model, messages=messages, stream=True, **extra)
        async for chunk in response:
            yield f"data: {chunk.model_dump_json()}\n\n".encode()
        yield b"data: [DONE]\n\n"
        await record_success(provider_id)
    except Exception as e:
        await record_failure(provider_id, billing_error=is_billing_error(str(e)))
        yield f'data: {{"error": "{str(e)}"}}\n\n'.encode()
