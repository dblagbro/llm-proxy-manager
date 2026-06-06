"""v5.1.0 / Batch C1 — Admin endpoints for the activity-log toggle.

Two endpoints under ``/api/admin/logging``:

  - GET /api/admin/logging/status   → current state + last-flip metadata
  - POST /api/admin/logging/toggle  → flip the toggle (body: {enabled, reason})

Cluster-sync semantics: the underlying ``system_settings`` row IS
replicated by the existing settings-sync loop, so a flip on www1
propagates to www2 + c1conv within one sync round (≤60s). The audit
row (in compliance_policy_changes) replicates separately via the
compliance-sync path.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.monitoring.logging_controls import (
    SETTING_KEY,
    is_logging_enabled,
    set_logging_enabled,
)

router = APIRouter(prefix="/api/admin/logging", tags=["admin", "compliance"])


class ToggleBody(BaseModel):
    enabled: bool
    reason: Optional[str] = Field(
        None,
        description=(
            "Free-text justification captured in the audit row. Strongly "
            "encouraged for OFF flips since auditors will want to know why."
        ),
        max_length=2000,
    )


@router.get("/status")
async def logging_status(
    db: AsyncSession = Depends(get_db),
    _admin: AdminUser = Depends(require_admin),
):
    """Return current state + the last toggle event for context."""
    enabled = await is_logging_enabled(db)

    # Surface the last flip (most recent compliance_policy_changes row
    # with scope='system' and a reason mentioning activity_logging).
    from app.models.db import CompliancePolicyChange
    rs = await db.execute(
        select(CompliancePolicyChange)
        .where(CompliancePolicyChange.scope == "system")
        .where(CompliancePolicyChange.reason.like("activity_logging%"))
        .order_by(desc(CompliancePolicyChange.changed_at))
        .limit(1)
    )
    last_flip = rs.scalar_one_or_none()
    last = None
    if last_flip:
        last = {
            "changed_at": last_flip.changed_at.isoformat() if last_flip.changed_at else None,
            "changed_by": last_flip.changed_by_user_id,
            "reason": last_flip.reason,
            "policy_change_id": last_flip.policy_change_id,
        }

    return {
        "enabled": enabled,
        "setting_key": SETTING_KEY,
        "last_flip": last,
    }


@router.post("/toggle")
async def logging_toggle(
    body: ToggleBody,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    """Flip the toggle. Returns the new state + the audit-row id.

    A no-op (toggling to the same state) still writes an audit row
    so the operator's intent is recorded.
    """
    result = await set_logging_enabled(
        db, enabled=body.enabled,
        actor=admin.username,
        reason=body.reason,
    )
    return result
