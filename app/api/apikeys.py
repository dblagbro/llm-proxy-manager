"""API key management endpoints."""
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import get_db
from app.models.db import ApiKey
from app.auth.admin import require_admin, AdminUser
from app.auth.keys import generate_api_key

router = APIRouter(prefix="/api/keys", tags=["api-keys"])


class KeyCreate(BaseModel):
    name: str
    key_type: str = "standard"  # standard|claude-code
    spending_cap_usd: Optional[float] = None
    rate_limit_rpm: Optional[int] = None
    daily_soft_cap_usd: Optional[float] = None
    daily_hard_cap_usd: Optional[float] = None
    hourly_cap_usd: Optional[float] = None
    semantic_cache_enabled: bool = False


class KeyUpdate(BaseModel):
    name: Optional[str] = None
    key_type: Optional[str] = None
    enabled: Optional[bool] = None
    spending_cap_usd: Optional[float] = None  # -1 to clear the cap
    rate_limit_rpm: Optional[int] = None       # -1 to clear the limit
    daily_soft_cap_usd: Optional[float] = None   # -1 to clear
    daily_hard_cap_usd: Optional[float] = None   # -1 to clear
    hourly_cap_usd: Optional[float] = None       # -1 to clear
    semantic_cache_enabled: Optional[bool] = None


@router.get("")
async def list_keys(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    keys = result.scalars().all()
    return [_serialize(k) for k in keys]


@router.post("")
async def create_key(
    body: KeyCreate,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    raw_key, key_hash = generate_api_key()
    key = ApiKey(
        id=secrets.token_hex(8),
        name=body.name,
        key_hash=key_hash,
        key_prefix=raw_key[:12],
        key_type=body.key_type,
        enabled=True,
        spending_cap_usd=body.spending_cap_usd,
        rate_limit_rpm=body.rate_limit_rpm,
        daily_soft_cap_usd=body.daily_soft_cap_usd,
        daily_hard_cap_usd=body.daily_hard_cap_usd,
        hourly_cap_usd=body.hourly_cap_usd,
        semantic_cache_enabled=body.semantic_cache_enabled,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    # Return raw key ONCE — never stored, never retrievable again
    result = _serialize(key)
    result["raw_key"] = raw_key
    return result


@router.patch("/{key_id}")
async def update_key(
    key_id: str,
    body: KeyUpdate,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    k = await _get_or_404(db, key_id)
    if body.name is not None:
        k.name = body.name
    if body.key_type is not None:
        k.key_type = body.key_type
    if body.enabled is not None:
        k.enabled = body.enabled
    if body.spending_cap_usd is not None:
        k.spending_cap_usd = None if body.spending_cap_usd < 0 else body.spending_cap_usd
    if body.rate_limit_rpm is not None:
        k.rate_limit_rpm = None if body.rate_limit_rpm < 0 else body.rate_limit_rpm
    if body.daily_soft_cap_usd is not None:
        k.daily_soft_cap_usd = None if body.daily_soft_cap_usd < 0 else body.daily_soft_cap_usd
    if body.daily_hard_cap_usd is not None:
        k.daily_hard_cap_usd = None if body.daily_hard_cap_usd < 0 else body.daily_hard_cap_usd
    if body.hourly_cap_usd is not None:
        k.hourly_cap_usd = None if body.hourly_cap_usd < 0 else body.hourly_cap_usd
    if body.semantic_cache_enabled is not None:
        k.semantic_cache_enabled = body.semantic_cache_enabled
    await db.commit()
    return _serialize(k)


class BulkDeleteBody(BaseModel):
    ids: list[str] = Field(default_factory=list)


@router.post("/bulk-delete")
async def bulk_delete_keys(
    body: BulkDeleteBody,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Delete multiple API keys in one call. Returns count deleted."""
    if not body.ids:
        return {"deleted": 0}
    result = await db.execute(select(ApiKey).where(ApiKey.id.in_(body.ids)))
    keys = result.scalars().all()
    count = 0
    for k in keys:
        await db.delete(k)
        count += 1
    await db.commit()
    return {"deleted": count, "requested": len(body.ids)}


@router.delete("/{key_id}")
async def delete_key(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    k = await _get_or_404(db, key_id)
    await db.delete(k)
    await db.commit()
    return {"ok": True}


async def _get_or_404(db: AsyncSession, key_id: str) -> ApiKey:
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    k = result.scalar_one_or_none()
    if not k:
        raise HTTPException(404, "API key not found")
    return k


def _serialize(k: ApiKey) -> dict:
    return {
        "id": k.id,
        "name": k.name,
        "key_prefix": k.key_prefix,
        "key_type": k.key_type,
        "enabled": k.enabled,
        "total_requests": k.total_requests,
        "total_tokens": k.total_tokens,
        "total_cost_usd": k.total_cost_usd,
        "spending_cap_usd": k.spending_cap_usd,
        "rate_limit_rpm": k.rate_limit_rpm,
        "daily_soft_cap_usd": k.daily_soft_cap_usd,
        "daily_hard_cap_usd": k.daily_hard_cap_usd,
        "hourly_cap_usd": k.hourly_cap_usd,
        "semantic_cache_enabled": bool(k.semantic_cache_enabled),
        "day_cost_usd": float(k.day_cost_usd or 0.0),
        "hour_cost_usd": float(k.hour_cost_usd or 0.0),
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }
