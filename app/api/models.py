"""GET /v1/models — OpenAI-compatible model listing."""
import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import get_db
from app.models.db import Provider, ModelCapability

router = APIRouter(tags=["models"])


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
            })

    return {"object": "list", "data": entries}
