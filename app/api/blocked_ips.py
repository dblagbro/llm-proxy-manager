"""v3.7.11 — admin endpoints for the IP block list.

  GET  /api/admin/blocked-ips         — list current blocks
  POST /api/admin/blocked-ips         — add an IP (body: {ip, reason?})
  DELETE /api/admin/blocked-ips/{ip}  — remove

Adds/removes propagate to peer nodes via cluster sync of the
``blocked_ips`` table. Within a single node, the middleware's in-memory
cache picks up the change on the next 30s refresh (or instantly if
admin code calls ``_clear_cache_for_tests`` — not exposed to API).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from datetime import datetime as _dt, timezone as _tz
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import BlockedIp

router = APIRouter(prefix="/api/admin", tags=["ip-block"])


class BlockedIpAdd(BaseModel):
    ip: str = Field(min_length=1, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=512)


def _serialize(b: BlockedIp) -> dict:
    return {
        "ip": b.ip,
        "reason": b.reason,
        "added_at": b.added_at.isoformat() if b.added_at else None,
        "added_by": b.added_by,
    }


@router.get("/blocked-ips")
async def list_blocked_ips(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    # v3.7.15 — filter tombstoned rows; the admin UI shows only live blocks.
    rs = await db.execute(
        select(BlockedIp)
        .where(BlockedIp.deleted_at.is_(None))
        .order_by(BlockedIp.added_at.desc())
    )
    return [_serialize(b) for b in rs.scalars().all()]


@router.post("/blocked-ips")
async def add_blocked_ip(
    body: BlockedIpAdd,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    """Add an IP to the block list. Idempotent — if already blocked,
    updates the reason + adds_by but doesn't 409.

    Light validation only — accepts any non-empty string up to 128 chars
    (operator might want to block CIDR ranges, hostnames, or IPv6 future
    extensions; we don't gate on format).
    """
    rs = await db.execute(select(BlockedIp).where(BlockedIp.ip == body.ip))
    existing = rs.scalar_one_or_none()
    if existing is not None:
        # v3.7.15 — re-arm a previously-tombstoned row by clearing
        # deleted_at + bumping added_at + recording the new actor.
        if existing.deleted_at is not None:
            existing.deleted_at = None
            existing.added_at = _dt.now(_tz.utc)
            existing.added_by = admin.username
            if body.reason:
                existing.reason = body.reason
        elif body.reason and body.reason != existing.reason:
            existing.reason = body.reason
            existing.added_by = admin.username
        await db.commit()
        # Force cache invalidation for this node
        from app.middleware.ip_block import _clear_cache_for_tests
        _clear_cache_for_tests()
        return {"ok": True, "already_blocked": True, "block": _serialize(existing)}
    block = BlockedIp(
        ip=body.ip,
        reason=body.reason,
        added_by=admin.username,
    )
    db.add(block)
    await db.commit()
    # Force cache invalidation for this node so the next request hits the block
    from app.middleware.ip_block import _clear_cache_for_tests
    _clear_cache_for_tests()
    return {"ok": True, "block": _serialize(block)}


@router.delete("/blocked-ips/{ip:path}")
async def remove_blocked_ip(
    ip: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Remove an IP from the block list. 404 if not currently blocked.

    v3.7.15 — soft-delete (sets ``deleted_at``) so the removal
    propagates through cluster sync as a tombstone. Peer nodes
    apply the tombstone and invalidate their middleware cache.
    Hard-delete remains available via the cluster janitor for old
    tombstones."""
    rs = await db.execute(
        select(BlockedIp)
        .where(BlockedIp.ip == ip)
        .where(BlockedIp.deleted_at.is_(None))
    )
    existing = rs.scalar_one_or_none()
    if existing is None:
        raise HTTPException(404, "IP not in block list")
    existing.deleted_at = _dt.now(_tz.utc)
    await db.commit()
    from app.middleware.ip_block import _clear_cache_for_tests
    _clear_cache_for_tests()
    return {"ok": True, "removed": ip}
