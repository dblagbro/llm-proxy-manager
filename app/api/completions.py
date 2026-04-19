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

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    llm_hint: Optional[str] = Header(None, alias="llm-hint"),
):
    # Accept Bearer token or x-api-key
    token = x_api_key
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    key_record = await verify_api_key(db, token)

    body = await request.json()
    messages_list = body.get("messages", [])
    stream = body.get("stream", False)
    model_hint = body.get("model")  # caller model hint (ignored for routing, used as override hint)
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

    resp_headers = {
        "X-Provider": route.provider.name,
        "LLM-Capability": route.capability_header,
    }

    try:
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
