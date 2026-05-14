"""v3.8.4 (#264) — admin endpoints for the tool capability prober.

Two endpoints:
- POST /api/providers/{id}/tool-prober-trigger  — fire one probe now
- GET  /api/providers/{id}/tool-probe-history   — last N probes for inspection
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import ModelToolProbe, Provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/providers", tags=["tool-prober"])


@router.post("/{provider_id}/tool-prober-trigger")
async def trigger_probe_now(
    provider_id: str,
    model_id: str | None = Query(default=None, description="Override model (defaults to provider.default_model)"),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Fire one probe immediately and return the result.

    Useful for smoke-testing newly-configured providers + capability
    audits. Bypasses the 24h cadence. Respects manual_override_until
    (refuses with 409 — operator should release the lock first if they
    want to probe a locked provider)."""
    rs = await db.execute(select(Provider).where(Provider.id == provider_id))
    provider = rs.scalar_one_or_none()
    if not provider or provider.deleted_at is not None:
        raise HTTPException(404, "Provider not found")
    if getattr(provider, "manual_override_until", None) is not None:
        raise HTTPException(
            409,
            "Provider is under manual override — release the lock to probe",
        )
    model = model_id or provider.default_model
    if not model:
        raise HTTPException(400, "provider has no default_model and no model_id override given")
    from app.monitoring.tool_capability_prober import probe_one_model, update_native_tools_from_rolling
    result = await probe_one_model(db, provider, model)
    new_val = await update_native_tools_from_rolling(db, provider.id, model)
    return {
        "ok": True,
        "probe": result,
        "native_tools_after_update": new_val,
    }


@router.get("/{provider_id}/tool-probe-history")
async def probe_history(
    provider_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Return the most recent N probes for a provider, newest first.
    Used by operator dashboards to see why native_tools flipped."""
    rs = await db.execute(select(Provider).where(Provider.id == provider_id))
    provider = rs.scalar_one_or_none()
    if not provider or provider.deleted_at is not None:
        raise HTTPException(404, "Provider not found")
    rows = (await db.execute(
        select(ModelToolProbe)
        .where(ModelToolProbe.provider_id == provider_id)
        .order_by(desc(ModelToolProbe.captured_at))
        .limit(limit)
    )).scalars().all()
    return [
        {
            "id": r.id,
            "model_id": r.model_id,
            "captured_at": r.captured_at.isoformat() if r.captured_at else None,
            "called": r.called,
            "parseable": r.parseable,
            "correct_args": r.correct_args,
            "response_format": r.response_format,
            "error": r.error,
            "raw_excerpt": r.raw_excerpt,
        }
        for r in rows
    ]
