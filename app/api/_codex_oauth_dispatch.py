"""
Codex OAuth dispatch helper for ``/v1/chat/completions`` (v3.0.15).

Branched off the regular litellm pipeline because the chatgpt.com codex
backend uses the OpenAI Responses API (not Chat Completions) and requires
an OAuth bearer + workspace header instead of a sk-... API key. Mirrors
the role of ``_complete_claude_oauth`` / ``_stream_claude_oauth`` for
the Anthropic OAuth path.

Translates Chat Completions request → Responses API → SSE → Chat
Completions response (stream or non-stream aggregate) so callers don't
need to know the difference.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.codex_oauth import CODEX_RESPONSES_URL, build_headers
from app.providers.codex_translate import (
    chat_completions_to_responses,
    responses_sse_to_chat_completions_sse,
    collect_responses_stream_into_completion,
)

logger = logging.getLogger(__name__)


def _account_id_for(provider) -> Optional[str]:
    cfg = provider.extra_config or {}
    if isinstance(cfg, dict):
        aid = cfg.get("chatgpt_account_id")
        if isinstance(aid, str) and aid:
            return aid
    return None


async def _refresh_if_needed(provider, db: AsyncSession) -> None:
    from app.providers.codex_oauth import is_token_expired
    if not is_token_expired(provider.oauth_expires_at):
        return
    if not provider.oauth_refresh_token:
        return
    from app.providers.codex_oauth_flow import refresh_and_persist
    try:
        await refresh_and_persist(provider, db)
    except Exception as e:
        logger.warning(
            "codex_oauth.preemptive_refresh_failed provider=%s err=%s",
            provider.id, e,
        )


async def _stream_codex_response_lines(
    provider, db: AsyncSession, upstream_body: dict,
) -> AsyncIterator[str]:
    """Open the codex POST, handling one 401 auto-refresh retry. Yields
    raw SSE lines from upstream. Owns the httpx client + response
    lifecycle via async context managers.

    v3.0.16: also reads x-codex-* rate-limit headers on success and
    parks the CB on 429/limit-exceeded so subscription-quota exhaustion
    flips to the next-priority provider for the rest of the window.
    """
    from app.providers.codex_ratelimit import (
        update_from_headers, detect_rate_limit_failure,
    )
    from app.routing.circuit_breaker import force_open

    refreshed_once = False
    while True:
        headers = build_headers(provider.api_key, chatgpt_account_id=_account_id_for(provider))
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST", CODEX_RESPONSES_URL, headers=headers, json=upstream_body,
            ) as resp:
                if resp.status_code == 401 and not refreshed_once and provider.oauth_refresh_token:
                    refreshed_once = True
                    from app.providers.codex_oauth_flow import refresh_and_persist
                    try:
                        await refresh_and_persist(provider, db)
                    except Exception as e:
                        raise HTTPException(401, f"codex-oauth refresh failed: {e}")
                    continue  # retry with rotated tokens
                if resp.status_code >= 400:
                    body = await resp.aread()
                    body_text = body[:300].decode(errors="replace")
                    # v3.0.16: detect "you hit the subscription cap" and park
                    # the CB until the window resets. Prevents the next 5
                    # retries from also slamming into the limit.
                    holddown = detect_rate_limit_failure(
                        resp.status_code, body_text, dict(resp.headers),
                    )
                    if holddown is not None:
                        try:
                            await force_open(provider.id)
                            logger.warning(
                                "codex_oauth.rate_limit_tripped provider=%s holddown_sec=%.0f status=%d",
                                provider.id, holddown, resp.status_code,
                            )
                        except Exception:
                            pass
                    raise HTTPException(
                        resp.status_code,
                        f"codex-oauth upstream {resp.status_code}: {body_text}",
                    )
                # Success — record subscription-quota state from headers
                try:
                    update_from_headers(provider.id, dict(resp.headers))
                except Exception as e:
                    logger.debug("codex_oauth.ratelimit_parse_failed err=%s", e)
                async for line in resp.aiter_lines():
                    yield line
                return


async def dispatch_codex_oauth(
    *,
    provider, body: dict, stream: bool, db: AsyncSession,
    resp_headers: dict,
) -> StreamingResponse | JSONResponse:
    """Translate + forward + translate back. Returns a FastAPI response."""
    if not provider.api_key:
        raise HTTPException(
            502, f"codex-oauth provider {provider.name!r} has no access token",
        )

    upstream_body = chat_completions_to_responses(body)
    model = body.get("model") or upstream_body.get("model") or "gpt-5.5"

    await _refresh_if_needed(provider, db)

    if stream:
        async def _translated():
            async for chunk in responses_sse_to_chat_completions_sse(
                _stream_codex_response_lines(provider, db, upstream_body),
                model=model,
            ):
                yield chunk

        resp_headers["X-Cache-Status"] = "bypass"
        resp_headers["X-Provider-Type"] = "codex-oauth"
        return StreamingResponse(
            _translated(),
            media_type="text/event-stream",
            headers=resp_headers,
        )

    # Non-stream: aggregate the SSE into a Chat Completions object
    result = await collect_responses_stream_into_completion(
        _stream_codex_response_lines(provider, db, upstream_body),
        model=model,
    )
    resp_headers["X-Provider-Type"] = "codex-oauth"
    return JSONResponse(content=result, headers=resp_headers)
