"""v5.15.0 Phase 1 (#508) — admin endpoints for per-account OAuth fan-out.

Mounted under ``/api/admin/providers/{provider_id}/oauth-accounts``.

Scope: CRUD on the ``provider_oauth_accounts`` table. Every edit writes
a ``compliance_policy_changes`` row (scope='per_provider',
target_id=provider_id) mirroring the v5.1.2 audit pattern. All writes
timestamp ``last_user_edit_at = time.time()`` so cluster-sync's LWW
merge treats operator edits as authoritative.

Phase 1 does NOT change dispatch — the accounts sit in the table for
operator management + audit, but ``Provider.api_key`` still drives
every request. Phase 2 (v5.15.1) flips dispatch.

Endpoints:

    GET    /oauth-accounts                — list accounts for provider
    POST   /oauth-accounts                — create an account
    PATCH  /oauth-accounts/{account_id}   — edit (enabled / label / tokens)
    DELETE /oauth-accounts/{account_id}   — soft-delete
    POST   /oauth-accounts/{account_id}/probe — manual liveness check (stub in Phase 1)

Operator-visible fields returned by GET/POST include the raw
``access_token`` and ``refresh_token``. This matches how the existing
``providers.api_key`` reads back — the admin UI needs it to render the
"copy token" button. If we ever wire tenant-scoped RBAC (v5.15+ #511),
this endpoint gets a scope check.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import (
    Provider, ProviderOAuthAccount, CompliancePolicyChange,
)

router = APIRouter(
    prefix="/api/admin/providers/{provider_id}/oauth-accounts",
    tags=["admin", "oauth-accounts"],
)


# ── Response / request models ─────────────────────────────────────────


class OAuthAccountOut(BaseModel):
    id: str
    provider_id: str
    label: str
    access_token: str
    refresh_token: Optional[str] = None
    oauth_expires_at: Optional[float] = None
    enabled: bool
    last_used_at: Optional[float] = None
    utilization_pct: Optional[float] = 0.0
    captured_via: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class OAuthAccountCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=255)
    access_token: str = Field(..., min_length=1)
    refresh_token: Optional[str] = None
    oauth_expires_at: Optional[float] = None
    enabled: bool = True
    captured_via: Optional[str] = "manual_paste"


class OAuthAccountPatch(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=255)
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    oauth_expires_at: Optional[float] = None
    enabled: Optional[bool] = None


# ── Helpers ───────────────────────────────────────────────────────────


def _serialize(acc: ProviderOAuthAccount) -> OAuthAccountOut:
    return OAuthAccountOut(
        id=acc.id,
        provider_id=acc.provider_id,
        label=acc.label,
        access_token=acc.access_token,
        refresh_token=acc.refresh_token,
        oauth_expires_at=acc.oauth_expires_at,
        enabled=acc.enabled,
        last_used_at=acc.last_used_at,
        utilization_pct=acc.utilization_pct,
        captured_via=acc.captured_via,
        created_at=acc.created_at.isoformat() if acc.created_at else None,
        updated_at=acc.updated_at.isoformat() if acc.updated_at else None,
    )


async def _get_provider_or_404(
    db: AsyncSession, provider_id: str,
) -> Provider:
    result = await db.execute(
        select(Provider).where(Provider.id == provider_id)
    )
    prov = result.scalar_one_or_none()
    if prov is None:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id} not found")
    return prov


async def _get_account_or_404(
    db: AsyncSession, provider_id: str, account_id: str,
) -> ProviderOAuthAccount:
    result = await db.execute(
        select(ProviderOAuthAccount).where(
            ProviderOAuthAccount.id == account_id,
            ProviderOAuthAccount.provider_id == provider_id,
        )
    )
    acc = result.scalar_one_or_none()
    if acc is None:
        raise HTTPException(
            status_code=404,
            detail=f"OAuth account {account_id} not found for provider {provider_id}",
        )
    return acc


async def _write_audit(
    db: AsyncSession,
    provider_id: str,
    reason: str,
    before: dict,
    after: dict,
    admin_user: AdminUser,
) -> None:
    """Match the v5.1.2 audit-row shape. Peer fan-out is intentionally
    deferred to Phase 2 (cluster-sync of provider_oauth_accounts landing
    in the same ship as dispatch flip) — Phase 1 marks each row as
    ``cluster_sync_status='local_only'`` so the audit trail is honest
    about what's replicated and what isn't."""
    row = CompliancePolicyChange(
        policy_change_id=f"paoa_{secrets.token_hex(12)}",
        scope="per_provider",
        target_id=provider_id,
        before_state=json.dumps(before, sort_keys=True, default=str),
        after_state=json.dumps(after, sort_keys=True, default=str),
        reason=reason,
        applied_to_peers=json.dumps([]),  # Phase 1 = local-only, no peer fan-out
        cluster_sync_status="local_only",
        changed_by_user_id=admin_user.username,
    )
    db.add(row)


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("", response_model=list[OAuthAccountOut])
async def list_accounts(
    provider_id: str,
    include_deleted: bool = False,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    await _get_provider_or_404(db, provider_id)
    q = select(ProviderOAuthAccount).where(
        ProviderOAuthAccount.provider_id == provider_id
    )
    if not include_deleted:
        q = q.where(ProviderOAuthAccount.deleted_at.is_(None))
    q = q.order_by(ProviderOAuthAccount.created_at)
    result = await db.execute(q)
    return [_serialize(a) for a in result.scalars().all()]


@router.post("", response_model=OAuthAccountOut, status_code=201)
async def create_account(
    provider_id: str,
    body: OAuthAccountCreate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    await _get_provider_or_404(db, provider_id)
    now_ts = time.time()
    acc = ProviderOAuthAccount(
        id=secrets.token_hex(8),
        provider_id=provider_id,
        label=body.label,
        access_token=body.access_token,
        refresh_token=body.refresh_token,
        oauth_expires_at=body.oauth_expires_at,
        enabled=body.enabled,
        captured_via=body.captured_via,
        last_user_edit_at=now_ts,
    )
    db.add(acc)
    await _write_audit(
        db, provider_id,
        reason="oauth_account_created",
        before={},
        after={
            "id": acc.id, "label": acc.label,
            "enabled": acc.enabled, "captured_via": acc.captured_via,
        },
        admin_user=admin,
    )
    await db.commit()
    await db.refresh(acc)
    return _serialize(acc)


@router.patch("/{account_id}", response_model=OAuthAccountOut)
async def patch_account(
    provider_id: str,
    account_id: str,
    body: OAuthAccountPatch,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    acc = await _get_account_or_404(db, provider_id, account_id)
    before = {
        "label": acc.label,
        "enabled": acc.enabled,
        "oauth_expires_at": acc.oauth_expires_at,
    }
    changed = False
    for field in ("label", "access_token", "refresh_token", "oauth_expires_at", "enabled"):
        val = getattr(body, field)
        if val is not None:
            setattr(acc, field, val)
            changed = True
    if not changed:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    acc.last_user_edit_at = time.time()
    after = {
        "label": acc.label,
        "enabled": acc.enabled,
        "oauth_expires_at": acc.oauth_expires_at,
    }
    await _write_audit(
        db, provider_id,
        reason="oauth_account_edited",
        before=before, after=after,
        admin_user=admin,
    )
    await db.commit()
    await db.refresh(acc)
    return _serialize(acc)


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    provider_id: str,
    account_id: str,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    acc = await _get_account_or_404(db, provider_id, account_id)
    if acc.deleted_at is not None:
        # Idempotent — already soft-deleted.
        return None
    from datetime import datetime, timezone
    acc.deleted_at = datetime.now(timezone.utc)
    acc.enabled = False
    acc.last_user_edit_at = time.time()
    await _write_audit(
        db, provider_id,
        reason="oauth_account_soft_deleted",
        before={"enabled": True, "deleted_at": None},
        after={"enabled": False, "deleted_at": acc.deleted_at.isoformat()},
        admin_user=admin,
    )
    await db.commit()
    return None


@router.post("/{account_id}/probe")
async def probe_account(
    provider_id: str,
    account_id: str,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    """Phase 1 stub — returns a fixed 'not_implemented' payload. Phase 2
    wires this to the existing per-provider keepalive check
    (``app/monitoring/keepalive.py::probe_provider``) but scoped to one
    specific token pair rather than the provider's live api_key.
    """
    acc = await _get_account_or_404(db, provider_id, account_id)
    return {
        "account_id": acc.id,
        "provider_id": provider_id,
        "status": "not_implemented_yet",
        "note": "Phase 1 (v5.15.0) has no live probe — will wire in v5.15.1 dispatch cutover.",
    }
