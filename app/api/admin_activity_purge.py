"""v5.1.1 / Batch C2 — Activity-log time-range bulk purge.

Lets the operator surgically delete activity_log rows in a time window
(use case: turn ON logging for an incident, then purge that window
after the incident so PII doesn't linger). Cluster-replicated via
peer fan-out so a purge on www1 also clears the same rows on www2 +
c1conv.

Two entry points:

  - ``POST /api/admin/activity-log/purge``  (admin-session auth)
       Operator-initiated. Performs local delete + audit row + fans
       out to peers via the HMAC-signed peer endpoint below.

  - ``POST /cluster/activity-log/purge``    (HMAC-auth)
       Peer-initiated. Same delete + audit but DOES NOT re-fan-out
       (would cause an infinite loop on cycle).

Safety nets:
  - Window cap (default 90 days) so a single click can't wipe years
  - Reason required, captured in the audit row
  - ``compliance_events`` is NOT in scope (audit-grade table; the
    operator should NEVER be able to purge those)
  - System-scope audit row in ``compliance_policy_changes``
  - The toggle audit rows in ``compliance_policy_changes`` are ALSO
    immune (would defeat the audit trail)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.cluster.auth import auth_headers_for, verify_cluster_request
from app.config import settings
from app.models.database import get_db
from app.models.db import ActivityLog, CompliancePolicyChange

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin", "compliance"])

# Largest single-call window. 90 days matches the api_key tombstone
# retention so any single purge is bounded to "the period a key could
# have been auto-restored anyway." Operators wanting longer windows
# call the endpoint multiple times in sequence.
_MAX_WINDOW_DAYS = 90


class PurgeBody(BaseModel):
    start_ts: float = Field(
        ...,
        description="Unix timestamp (UTC seconds) — inclusive start of the purge window.",
    )
    end_ts: float = Field(
        ...,
        description="Unix timestamp (UTC seconds) — exclusive end of the purge window.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description=(
            "Free-text justification — captured in the compliance "
            "audit row. Required."
        ),
    )


def _validate_window(start_ts: float, end_ts: float) -> tuple[datetime, datetime]:
    if not (start_ts > 0 and end_ts > 0):
        raise HTTPException(400, "start_ts and end_ts must be positive Unix timestamps")
    if end_ts <= start_ts:
        raise HTTPException(400, "end_ts must be strictly greater than start_ts")
    if (end_ts - start_ts) > _MAX_WINDOW_DAYS * 86400:
        raise HTTPException(
            400,
            f"window exceeds the {_MAX_WINDOW_DAYS}-day cap. Issue "
            f"multiple sequential purges if you need a longer span.",
        )
    return (
        datetime.fromtimestamp(start_ts, tz=timezone.utc).replace(tzinfo=None),
        datetime.fromtimestamp(end_ts, tz=timezone.utc).replace(tzinfo=None),
    )


async def _do_local_purge(
    db: AsyncSession,
    start_dt: datetime,
    end_dt: datetime,
    actor: str,
    reason: str,
    source: str,  # "admin" | "peer"
) -> tuple[int, str]:
    """Perform the local delete + write the audit row. Returns
    (deleted_count, audit_policy_change_id)."""
    import secrets

    # Count first so the audit row records what was removed.
    pre_count = (await db.execute(
        select(func.count(ActivityLog.id))
        .where(ActivityLog.created_at >= start_dt)
        .where(ActivityLog.created_at < end_dt)
    )).scalar() or 0

    # Delete the window.
    await db.execute(
        delete(ActivityLog)
        .where(ActivityLog.created_at >= start_dt)
        .where(ActivityLog.created_at < end_dt)
    )

    # Audit row in compliance_policy_changes. Itself NEVER purgeable.
    audit_id = secrets.token_urlsafe(16)
    summary = (
        f"activity_log time-range purge: window=[{start_dt.isoformat()}, "
        f"{end_dt.isoformat()}) UTC; deleted={pre_count}; "
        f"actor={actor}; source={source}; reason: {reason}"
    )
    db.add(CompliancePolicyChange(
        policy_change_id=audit_id,
        changed_at=datetime.utcnow(),
        changed_by_user_id=actor,
        scope="system",
        target_id=None,
        before_state=json.dumps({"activity_log_rows_in_window": pre_count}),
        after_state=json.dumps({"activity_log_rows_in_window": 0}),
        reason=summary,
        applied_to_peers=json.dumps([]),
        pending_peers=None,
        cluster_sync_status="local_only",
    ))
    await db.commit()
    logger.warning(
        "activity_log.purge_done deleted=%d window=[%s,%s) actor=%s source=%s",
        pre_count, start_dt, end_dt, actor, source,
    )
    return pre_count, audit_id


async def _fan_out_to_peers(
    start_ts: float, end_ts: float, reason: str, actor: str,
) -> list[dict]:
    """Push the purge to every reachable peer using HMAC auth. Returns
    per-peer result records for the response body."""
    from app.cluster.manager import _peers
    results: list[dict] = []
    if not _peers:
        return results
    body = {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "reason": reason,
        "origin_actor": actor,
    }
    headers = auth_headers_for(body)
    for peer in list(_peers.values()):
        url = f"{peer.url.rstrip('/')}/cluster/activity-log/purge"
        try:
            async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                results.append({
                    "peer_id": peer.id,
                    "peer_url": peer.url,
                    "status": "ok",
                    "deleted": int(data.get("deleted", 0)),
                })
            else:
                results.append({
                    "peer_id": peer.id,
                    "peer_url": peer.url,
                    "status": f"http_{resp.status_code}",
                    "deleted": 0,
                    "error": (resp.text or "")[:200],
                })
        except Exception as exc:
            results.append({
                "peer_id": peer.id,
                "peer_url": peer.url,
                "status": "unreachable",
                "deleted": 0,
                "error": str(exc)[:200],
            })
    return results


@router.post("/api/admin/activity-log/purge")
async def admin_purge_activity_log(
    body: PurgeBody,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    """Operator-initiated purge. Authenticated via admin session.

    Performs the local delete + audit row + fans out to peers. The
    response includes the per-peer outcome so the operator can see
    if any peer was unreachable (and re-run later if needed).
    """
    start_dt, end_dt = _validate_window(body.start_ts, body.end_ts)
    local_deleted, audit_id = await _do_local_purge(
        db, start_dt, end_dt, admin.username, body.reason, source="admin",
    )
    peer_results = await _fan_out_to_peers(
        body.start_ts, body.end_ts, body.reason, admin.username,
    )
    return {
        "ok": True,
        "local": {"deleted": local_deleted, "audit_id": audit_id},
        "peers": peer_results,
    }


@router.post("/cluster/activity-log/purge")
async def peer_purge_activity_log(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Peer-to-peer purge propagation. HMAC-authenticated; does NOT
    re-fan-out (would loop)."""
    raw = await request.body()
    sig = request.headers.get("X-Cluster-Sig", "")
    if not verify_cluster_request(raw, sig):
        raise HTTPException(401, "invalid HMAC signature")
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    try:
        start_ts = float(body["start_ts"])
        end_ts = float(body["end_ts"])
        reason = str(body.get("reason") or "")
        origin_actor = str(body.get("origin_actor") or "unknown@peer")
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"missing/invalid field: {exc}")
    start_dt, end_dt = _validate_window(start_ts, end_ts)
    deleted, audit_id = await _do_local_purge(
        db, start_dt, end_dt, origin_actor, reason, source="peer",
    )
    return {"ok": True, "deleted": deleted, "audit_id": audit_id}
