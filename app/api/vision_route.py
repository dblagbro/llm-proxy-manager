"""Wave 5 #25 — Vision-to-text routing.

When the selected provider can't handle vision and the request has images,
route each image through a vision-capable provider to get a caption +
extracted text, then inject back as plain text blocks. This preserves
information the strip_images_* helpers throw away.

Safety:
- Only one VLM call per image per request (no recursion).
- 15s per-image timeout; failed images degrade to the legacy placeholder.
- Caller-configurable via `vision_route_enabled` + explicit provider pick.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Provider

logger = logging.getLogger(__name__)


_VISION_PROMPT = (
    "Describe this image concisely. If it contains text, transcribe it verbatim. "
    "Output format:\n"
    "DESCRIPTION: <1–2 sentences summarizing the visual content>\n"
    "TEXT: <verbatim text transcription if any, else 'none'>\n"
)


async def pick_vision_provider(
    db: AsyncSession, exclude_provider_id: Optional[str] = None
) -> Optional[Provider]:
    """Find a vision-capable, enabled provider. Prefer economy cost tier."""
    from app.routing.circuit_breaker import is_available

    result = await db.execute(select(Provider).where(Provider.enabled == True).order_by(Provider.priority))
    candidates = result.scalars().all()
    # Prefer providers explicitly marked as vision-capable via a non-empty
    # default_model heuristic (gemini-*, gpt-4o*, claude-3-*) — this is a
    # fallback when ModelCapability lookups are not cached.
    vision_hints = ("gemini", "gpt-4o", "gpt-4-vision", "claude-3", "claude-sonnet", "claude-opus", "haiku")
    for p in candidates:
        if exclude_provider_id and p.id == exclude_provider_id:
            continue
        model = (p.default_model or "").lower()
        if any(h in model for h in vision_hints):
            if await is_available(p.id):
                return p
    return None


async def _describe_one_image_anthropic_block(
    block: dict, vision_provider: Provider
) -> str:
    """Call the chosen provider with a single image; return DESCRIPTION+TEXT."""
    from app.routing.router import build_litellm_model, build_litellm_kwargs
    from app.routing.retry import acompletion_with_retry

    model = build_litellm_model(vision_provider)
    kwargs = build_litellm_kwargs(vision_provider)

    # Translate Anthropic image block → OpenAI image_url (litellm accepts
    # OpenAI-style messages for Anthropic/Gemini/OpenAI alike).
    src = block.get("source", {})
    if src.get("type") == "base64":
        media = src.get("media_type", "image/png")
        data = src.get("data", "")
        data_url = f"data:{media};base64,{data}"
    elif src.get("type") == "url":
        data_url = src.get("url", "")
    else:
        return "[image — unknown source format]"

    try:
        resp = await asyncio.wait_for(
            acompletion_with_retry(
                model=model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]}],
                max_tokens=300,
                stream=False,
                **kwargs,
            ),
            timeout=15.0,
        )
        return resp.choices[0].message.content or "[image — vision call returned empty]"
    except (asyncio.TimeoutError, Exception) as exc:
        logger.warning("vision_route.describe_failed %s", exc)
        return f"[image — vision call failed: {type(exc).__name__}]"


async def _describe_one_image_openai_part(
    part: dict, vision_provider: Provider
) -> str:
    """OpenAI image_url parts are already in the right shape for another
    OpenAI-compatible provider; just call through."""
    from app.routing.router import build_litellm_model, build_litellm_kwargs
    from app.routing.retry import acompletion_with_retry

    model = build_litellm_model(vision_provider)
    kwargs = build_litellm_kwargs(vision_provider)

    image_url = part.get("image_url", {})
    if isinstance(image_url, str):
        image_url = {"url": image_url}

    try:
        resp = await asyncio.wait_for(
            acompletion_with_retry(
                model=model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": image_url},
                ]}],
                max_tokens=300,
                stream=False,
                **kwargs,
            ),
            timeout=15.0,
        )
        return resp.choices[0].message.content or "[image — vision call returned empty]"
    except (asyncio.TimeoutError, Exception) as exc:
        logger.warning("vision_route.describe_failed %s", exc)
        return f"[image — vision call failed: {type(exc).__name__}]"


async def transcribe_anthropic(
    messages: list[dict], db: AsyncSession, exclude_provider_id: Optional[str] = None
) -> tuple[list[dict], int]:
    """Replace Anthropic image blocks with transcribed text blocks in parallel.

    Returns (new_messages, images_transcribed).
    """
    vp = await pick_vision_provider(db, exclude_provider_id)
    if vp is None:
        # No vision-capable alternate — fall back to the legacy strip
        from app.api.image_utils import strip_images_anthropic
        return strip_images_anthropic(messages), 0

    tasks: list[tuple[int, int, asyncio.Task]] = []
    out = [dict(m) for m in messages]
    for mi, msg in enumerate(out):
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "image":
                tasks.append((mi, bi, asyncio.create_task(
                    _describe_one_image_anthropic_block(block, vp)
                )))
    if not tasks:
        return out, 0

    for mi, bi, t in tasks:
        description = await t
        out[mi] = dict(out[mi])
        new_content = list(out[mi].get("content", []))
        new_content[bi] = {"type": "text", "text": f"[image-description via {vp.name}]\n{description}"}
        out[mi]["content"] = new_content
    return out, len(tasks)


async def transcribe_openai(
    messages: list[dict], db: AsyncSession, exclude_provider_id: Optional[str] = None
) -> tuple[list[dict], int]:
    vp = await pick_vision_provider(db, exclude_provider_id)
    if vp is None:
        from app.api.image_utils import strip_images_openai
        return strip_images_openai(messages), 0

    tasks: list[tuple[int, int, asyncio.Task]] = []
    out = [dict(m) for m in messages]
    for mi, msg in enumerate(out):
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for bi, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "image_url":
                tasks.append((mi, bi, asyncio.create_task(
                    _describe_one_image_openai_part(part, vp)
                )))
    if not tasks:
        return out, 0

    for mi, bi, t in tasks:
        description = await t
        out[mi] = dict(out[mi])
        new_content = list(out[mi].get("content", []))
        new_content[bi] = {"type": "text", "text": f"[image-description via {vp.name}]\n{description}"}
        out[mi]["content"] = new_content
    return out, len(tasks)
