"""v5.0.0 — compliance admin + user-facing endpoints (spec §8.1).

Endpoints:

- ``GET/POST /api/debug/echo-client`` — sandbox echo (key.debug_echo_enabled
  gated).
- ``GET /api/admin/cluster/compliance-ready`` — preflight before a policy
  change. Returns per-peer health + state-consistency.
- ``GET /api/admin/compliance-events`` — JSON or CSV stream.
- ``GET /api/admin/compliance-policy-changes`` — recent policy-edit history.
- ``GET /api/me/compliance`` — per-key transparency view.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import require_admin, AdminUser
from app.auth.keys import ApiKeyRecord, resolve_api_key_dep
from app.compliance import (
    KNOWN_COMPANIES,
    detect_client_company,
    emit_event,
    generate_audit_id,
    get_effective_blocklist,
)
from app.compliance.policy import get_custom_companies
from app.config_runtime import get_setting
from app.models.database import get_db
from app.models.db import ApiKey, ComplianceEvent, CompliancePolicyChange

router = APIRouter(tags=["compliance"])

_DISCLAIMER_URL = "https://www.voipguru.org/llm-proxy2/docs/compliance"

# CSV columns required by spec §3.2 (exact order).
_CSV_COLUMNS = [
    "audit_id", "api_key_id", "event_type", "requested_at",
    "requested_model", "served_model", "served_provider_id",
    "blocked_company", "reason_code", "client_user_agent", "http_status",
]


# ── Sandbox echo ─────────────────────────────────────────────────────


@router.get("/api/debug/echo-client")
@router.post("/api/debug/echo-client")
async def debug_echo(
    request: Request,
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(resolve_api_key_dep()),
):
    """Decision 31 — sandbox-only echo so operators can verify what the
    proxy actually saw on the wire (UA, identity headers, matched product).
    Production keys leave ``debug_echo_enabled=False``."""
    if not key.debug_echo_enabled:
        raise HTTPException(403, {"error": "debug_echo_not_enabled"})
    ua = request.headers.get("user-agent", "")
    custom = get_custom_companies() or None
    detection = detect_client_company(ua, custom_companies=custom)
    blocklist = await get_effective_blocklist(db, key.id)
    matched_company = detection[0] if detection else None
    return {
        "request_id": f"echo_{generate_audit_id()}",
        "echoed_at": datetime.utcnow().isoformat() + "Z",
        "user_agent": ua,
        "headers_seen": {
            "x-coordinator-client": request.headers.get("x-coordinator-client"),
            "x-coordinator-profile": request.headers.get("x-coordinator-profile"),
            "x-coordinator-client-version": request.headers.get("x-coordinator-client-version"),
            "x-coordinator-upstream-cli": request.headers.get("x-coordinator-upstream-cli"),
            "llm-hint": request.headers.get("llm-hint"),
        },
        "matched_client_product": detection[2] if detection else None,
        "matched_pattern": detection[1] if detection else None,
        "would_451": bool(matched_company and matched_company in blocklist),
        "api_key_policy": {
            "blocked_companies": list(key.blocked_companies or []),
            "effective_blocked_companies": sorted(blocklist),
        },
        "policy_active_at": datetime.utcnow().isoformat() + "Z",
    }


# ── Cluster readiness preflight ──────────────────────────────────────


@router.get("/api/admin/cluster/compliance-ready")
async def cluster_compliance_ready(_: AdminUser = Depends(require_admin)):
    """Decision 32 — preflight before any compliance policy change. The
    admin UI calls this and refuses to ship the edit if not all peers are
    healthy + state-consistent.

    Returns conservative defaults (``ready_for_policy_change=False``) on
    any internal exception so an unexpected failure never tricks the UI
    into thinking the cluster is ready."""
    try:
        from app.cluster.manager import peers as cluster_peers
        from app.config import settings

        peer_dicts = []
        all_healthy = True
        hashes: set[str] = set()
        for peer in list(cluster_peers.values()):
            healthy = peer.status == "healthy"
            if not healthy:
                all_healthy = False
            # No state-hash endpoint yet — surface NULL; downstream cluster
            # consistency check folds to True when only one node reports.
            peer_dicts.append({
                "name": peer.name,
                "healthy": healthy,
                "last_sync_at": (
                    datetime.utcfromtimestamp(peer.last_heartbeat).isoformat() + "Z"
                    if peer.last_heartbeat else None
                ),
                "current_state_hash": None,
            })
        cluster_size = 1 + len(peer_dicts)  # self + peers
        quorum_size = max(0, cluster_size - 1)
        consistent = len(hashes) <= 1
        return {
            "ready_for_policy_change": bool(all_healthy and consistent),
            "cluster_size": cluster_size,
            "peers": peer_dicts,
            "quorum_size": quorum_size,
            "current_compliance_state_consistent": consistent,
            # No global in-flight counter today — surface conservative 0s.
            # When v5.0.0 adds the active-request tracker these flip to real numbers.
            "active_streams_cluster_wide": 0,
            "active_requests_cluster_wide": 0,
            "oldest_active_request_started_at": None,
            "cluster_enabled": bool(getattr(settings, "cluster_enabled", False)),
        }
    except Exception as exc:
        # Fail-closed for safety: tell the UI we're NOT ready.
        return {
            "ready_for_policy_change": False,
            "cluster_size": 1,
            "peers": [],
            "quorum_size": 0,
            "current_compliance_state_consistent": False,
            "active_streams_cluster_wide": 0,
            "active_requests_cluster_wide": 0,
            "oldest_active_request_started_at": None,
            "error": str(exc),
        }


# ── Audit query (JSON + CSV) ─────────────────────────────────────────


def _event_row_to_dict(row: ComplianceEvent) -> dict:
    return {
        "audit_id": row.audit_id,
        "api_key_id": row.api_key_id,
        "event_type": row.event_type,
        "requested_at": row.requested_at.isoformat() + "Z" if row.requested_at else None,
        "requested_model": row.requested_model,
        "served_model": row.served_model,
        "served_provider_id": row.served_provider_id,
        "blocked_company": row.blocked_company,
        "reason_code": row.reason_code,
        "client_user_agent": row.client_user_agent,
        "http_status": row.http_status,
        "matched_pattern": row.matched_pattern,
        "client_identity": (
            json.loads(row.client_identity) if row.client_identity else None
        ),
        "policy_active_since": (
            row.policy_active_since.isoformat() + "Z" if row.policy_active_since else None
        ),
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
    }


@router.get("/api/admin/compliance-events")
async def admin_compliance_events(
    api_key_id: Optional[str] = None,
    event_type: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    format: str = "json",
    limit: int = 1000,
    cursor: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Decision 15 + 24 — admin-only export. CSV column order matches
    spec §3.2 exactly so external audit tooling has a stable schema.

    Filters: ``api_key_id``, ``event_type``, ``start`` / ``end``
    (ISO-8601 timestamps). Limit caps at 10_000 to protect the DB."""
    if format not in ("json", "csv"):
        raise HTTPException(400, "format must be 'json' or 'csv'")
    limit = max(1, min(int(limit or 1000), 10_000))
    q = select(ComplianceEvent)
    conds = []
    if api_key_id:
        conds.append(ComplianceEvent.api_key_id == api_key_id)
    if event_type:
        conds.append(ComplianceEvent.event_type == event_type)
    if start:
        try:
            conds.append(ComplianceEvent.created_at >= datetime.fromisoformat(start.replace("Z", "")))
        except ValueError:
            raise HTTPException(400, f"Invalid 'start' timestamp: {start}")
    if end:
        try:
            conds.append(ComplianceEvent.created_at < datetime.fromisoformat(end.replace("Z", "")))
        except ValueError:
            raise HTTPException(400, f"Invalid 'end' timestamp: {end}")
    if cursor:
        conds.append(ComplianceEvent.id < cursor)
    if conds:
        q = q.where(and_(*conds))
    q = q.order_by(ComplianceEvent.id.desc()).limit(limit)
    rs = await db.execute(q)
    rows = rs.scalars().all()

    if format == "csv":
        def _stream():
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(_CSV_COLUMNS)
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)
            for row in rows:
                writer.writerow([
                    row.audit_id, row.api_key_id, row.event_type,
                    row.requested_at.isoformat() + "Z" if row.requested_at else "",
                    row.requested_model or "",
                    row.served_model or "",
                    row.served_provider_id or "",
                    row.blocked_company or "",
                    row.reason_code or "",
                    row.client_user_agent or "",
                    row.http_status or 0,
                ])
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)
        return StreamingResponse(
            _stream(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="compliance-events.csv"'},
        )

    next_cursor = rows[-1].id if rows and len(rows) == limit else None
    return {
        "events": [_event_row_to_dict(r) for r in rows],
        "next_cursor": next_cursor,
    }


# ── Policy change history ────────────────────────────────────────────


def _policy_change_to_dict(row: CompliancePolicyChange) -> dict:
    return {
        "policy_change_id": row.policy_change_id,
        "changed_at": row.changed_at.isoformat() + "Z" if row.changed_at else None,
        "changed_by_user_id": row.changed_by_user_id,
        "scope": row.scope,
        "target_id": row.target_id,
        "before_state": json.loads(row.before_state) if row.before_state else None,
        "after_state": json.loads(row.after_state) if row.after_state else None,
        "reason": row.reason,
        "applied_to_peers": (
            json.loads(row.applied_to_peers) if row.applied_to_peers else []
        ),
        "pending_peers": (
            json.loads(row.pending_peers) if row.pending_peers else []
        ),
        "cluster_sync_status": row.cluster_sync_status,
    }


@router.get("/api/admin/compliance-policy-changes")
async def admin_compliance_policy_changes(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    limit = max(1, min(int(limit or 100), 1000))
    rs = await db.execute(
        select(CompliancePolicyChange)
        .order_by(CompliancePolicyChange.id.desc())
        .limit(limit)
    )
    return {"changes": [_policy_change_to_dict(r) for r in rs.scalars().all()]}


# ── User transparency view ───────────────────────────────────────────


@router.get("/api/me/compliance")
async def me_compliance(
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(resolve_api_key_dep()),
):
    """Decision 24 — per-key transparency. Any authenticated key may
    read its own effective compliance posture."""
    blocklist = await get_effective_blocklist(db, key.id)
    system_block_raw = get_setting("compliance_system_blocked_companies", []) or []
    if isinstance(system_block_raw, str):
        try:
            system_block_raw = json.loads(system_block_raw)
        except Exception:
            system_block_raw = []
    if not isinstance(system_block_raw, list):
        system_block_raw = []

    since = datetime.utcnow() - timedelta(hours=24)
    subs_rs = await db.execute(
        select(func.count(ComplianceEvent.id)).where(
            ComplianceEvent.api_key_id == key.id,
            ComplianceEvent.event_type.in_((
                "model_substitution", "provider_substitution",
            )),
            ComplianceEvent.created_at >= since,
        )
    )
    refusals_rs = await db.execute(
        select(func.count(ComplianceEvent.id)).where(
            ComplianceEvent.api_key_id == key.id,
            ComplianceEvent.http_status == 451,
            ComplianceEvent.created_at >= since,
        )
    )
    last_change_rs = await db.execute(
        select(CompliancePolicyChange).where(
            (CompliancePolicyChange.scope == "system")
            | (CompliancePolicyChange.target_id == key.id)
        ).order_by(CompliancePolicyChange.id.desc()).limit(1)
    )
    last_change = last_change_rs.scalar_one_or_none()
    last_change_payload = None
    if last_change:
        last_change_payload = {
            "changed_at": (
                last_change.changed_at.isoformat() + "Z" if last_change.changed_at else None
            ),
            "reason": last_change.reason,
            "changed_by_user_id": last_change.changed_by_user_id,
        }

    return {
        "per_key_blocked_companies": list(key.blocked_companies or []),
        "system_blocked_companies": [str(x) for x in system_block_raw if x],
        "effective_blocked_companies": sorted(blocklist),
        "allowed_paths": list(key.allowed_paths) if key.allowed_paths else None,
        "debug_echo_enabled": bool(key.debug_echo_enabled),
        "recent_substitutions_24h": int(subs_rs.scalar() or 0),
        "recent_451_count_24h": int(refusals_rs.scalar() or 0),
        "last_policy_change": last_change_payload,
        "compliance_disclaimer_url": _DISCLAIMER_URL,
        "policy_active_at": datetime.utcnow().isoformat() + "Z",
    }
