"""Admin "needs attention" provider view + guided retest/reset.

HTTP shape only; logic lives in ``app.routing.attention``. Per-node: the
breaker reset affects this node only (the peer re-probes on its own).
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.providers import _get_or_404
from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import Provider
from app.routing.attention import build_attention_list, retest_and_reset

router = APIRouter(prefix="/api/provider-attention", tags=["providers"])


@router.get("")
async def list_attention(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    providers = (await db.execute(select(Provider))).scalars().all()
    return {"items": build_attention_list(list(providers)), "scope": "this-node"}


@router.post("/{provider_id}/retest")
async def retest(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    p = await _get_or_404(db, provider_id)
    return await retest_and_reset(p)
