"""GET /v1/models — OpenAI-compatible model listing."""
import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import get_db
from app.models.db import Provider, ModelCapability

router = APIRouter(tags=["models"])


# v3.0.23 (Q10): infer model "kind" from name patterns so callers can filter
# their dropdowns to the right surface (chat vs embedding vs image vs audio).
# Accurate-enough heuristics for the major providers; specific edge cases can
# be encoded as exact matches over time.
def _infer_kind(model_id: str) -> str:
    m = (model_id or "").lower()
    # Embeddings — OpenAI / Cohere / Google
    if any(p in m for p in ("text-embedding", "embed-", "/embed", "embedding-")):
        return "embedding"
    # Image generation / edit
    if any(p in m for p in ("dall-e", "stable-diffusion", "imagen", "midjourney", "/imag", "gpt-image")):
        return "image"
    # Audio (TTS / STT)
    if any(p in m for p in ("whisper", "tts-", "/tts", "voice-")):
        return "audio"
    # Vision-only (rare; usually chat models that happen to also do vision)
    # Default to chat — covers GPT/Claude/Gemini/Grok/etc.
    return "chat"


@router.get("/v1/models")
async def list_models(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Provider).where(Provider.enabled == True).order_by(Provider.priority)
    )
    providers = result.scalars().all()

    seen: set[str] = set()
    entries: list[dict] = []

    for p in providers:
        caps_result = await db.execute(
            select(ModelCapability).where(ModelCapability.provider_id == p.id)
        )
        caps = caps_result.scalars().all()

        model_ids = [c.model_id for c in caps]
        if p.default_model and p.default_model not in model_ids:
            model_ids.insert(0, p.default_model)

        for mid in model_ids:
            if mid in seen:
                continue
            seen.add(mid)
            entries.append({
                "id": mid,
                "object": "model",
                "created": int(time.time()),
                "owned_by": p.name,
                # v3.0.23 (Q10): kind tag for client-side filtering.
                # One of: chat, embedding, image, audio.
                "kind": _infer_kind(mid),
            })

    return {"object": "list", "data": entries}
