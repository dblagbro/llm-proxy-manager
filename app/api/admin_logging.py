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


# ── v5.1.2 / Batch C3 — retention editable in WebUI ────────────────


class RetentionEdit(BaseModel):
    # Per-severity retention overrides. Pass null to CLEAR an override
    # (falls back to the env default).
    info_days:    Optional[int] = Field(None, ge=1, le=36500)
    warning_days: Optional[int] = Field(None, ge=1, le=36500)
    error_days:   Optional[int] = Field(None, ge=1, le=36500)
    # Explicit flags so the operator can request "clear this override"
    # without ambiguity. When True, the corresponding *_days is
    # treated as None regardless of the parsed value.
    clear_info:    bool = False
    clear_warning: bool = False
    clear_error:   bool = False
    reason: Optional[str] = Field(
        None, max_length=2000,
        description="Free-text justification captured in the audit row.",
    )


@router.get("/retention")
async def retention_status(
    db: AsyncSession = Depends(get_db),
    _admin: AdminUser = Depends(require_admin),
):
    from app.monitoring.retention_settings import (
        refresh_from_db, current_state,
        KEY_INFO, KEY_WARNING, KEY_ERROR,
    )
    # Always refresh so the UI shows the authoritative state.
    await refresh_from_db(db)
    state = current_state()
    # Reshape for friendlier client consumption.
    return {
        "info":    state[KEY_INFO],
        "warning": state[KEY_WARNING],
        "error":   state[KEY_ERROR],
    }


@router.post("/retention")
async def retention_edit(
    body: RetentionEdit,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    """Set one or more retention overrides. Each non-None field that
    differs from the current override is persisted + audit-rowed.
    ``clear_*`` flags clear that key's override (falls back to env).
    """
    from app.monitoring.retention_settings import (
        set_retention, current_state, refresh_from_db,
        KEY_INFO, KEY_WARNING, KEY_ERROR,
    )
    await refresh_from_db(db)
    audit_ids: list[str] = []

    async def _apply(key: str, new_days: Optional[int]):
        result = await set_retention(
            db, key=key, days=new_days,
            actor=admin.username, reason=body.reason,
        )
        audit_ids.append(result["audit_id"])

    if body.clear_info or body.info_days is not None:
        await _apply(KEY_INFO, None if body.clear_info else body.info_days)
    if body.clear_warning or body.warning_days is not None:
        await _apply(KEY_WARNING, None if body.clear_warning else body.warning_days)
    if body.clear_error or body.error_days is not None:
        await _apply(KEY_ERROR, None if body.clear_error else body.error_days)

    state = current_state()
    return {
        "ok": True,
        "audit_ids": audit_ids,
        "current": {
            "info":    state[KEY_INFO],
            "warning": state[KEY_WARNING],
            "error":   state[KEY_ERROR],
        },
    }
