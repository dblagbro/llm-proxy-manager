"""v5.2.0 / Batch V1 — Admin endpoints for the LLM emergency stop.

Mirrors ``admin_logging.py`` (the v5.1.0 activity-log toggle). Two
endpoints under ``/api/admin/llm-emergency-stop``:

  - GET  /api/admin/llm-emergency-stop/status  — current state + last flip
  - POST /api/admin/llm-emergency-stop/toggle  — flip (body: {enabled, reason})

Cluster-sync: the backing ``system_settings`` row replicates via the
existing settings-sync loop within one round (~60s). The audit row in
``compliance_policy_changes`` replicates via the compliance-sync path.
Cache invalidation on peer arrival is via TTL (30s), matching the
logging-controls pattern.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.monitoring.llm_emergency_stop import (
    SETTING_KEY,
    is_llm_stopped,
    set_llm_stopped,
)

router = APIRouter(prefix="/api/admin/llm-emergency-stop", tags=["admin", "compliance"])


class ToggleBody(BaseModel):
    enabled: bool
    reason: Optional[str] = Field(
        None, max_length=2000,
        description=(
            "Free-text justification captured in the audit row. "
            "Strongly encouraged for ENGAGE flips so auditors can "
            "reconstruct the incident timeline."
        ),
    )


@router.get("/status")
async def llm_emergency_status(
    db: AsyncSession = Depends(get_db),
    _admin: AdminUser = Depends(require_admin),
):
    """Current state + last flip metadata."""
    enabled = await is_llm_stopped(db)

    from app.models.db import CompliancePolicyChange
    rs = await db.execute(
        select(CompliancePolicyChange)
        .where(CompliancePolicyChange.scope == "system")
        .where(CompliancePolicyChange.reason.like("llm_emergency_stop%"))
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
async def llm_emergency_toggle(
    body: ToggleBody,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    """Engage or disengage the emergency stop. Returns the new state +
    the audit row id. A no-op (re-flipping to the same state) still
    writes an audit row so the operator's intent is recorded.
    """
    return await set_llm_stopped(
        db, enabled=body.enabled,
        actor=admin.username, reason=body.reason,
    )
