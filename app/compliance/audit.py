"""Audit trail writers + integrity hash (v5.0.0).

Three functions:

- ``emit_event`` — writes one ``ComplianceEvent`` row. Called from every
  compliance enforcement path (substitution, 451, 503, cache filter,
  memory filter, path-not-allowed, grandfathered-in-flight).
- ``emit_policy_change`` — writes one ``CompliancePolicyChange`` row.
  Called from settings_api.py + apikeys.py when an admin edits compliance
  policy. Records the cluster fan-out outcome (which peers acked, which
  were pending at quorum).
- ``compute_daily_integrity_hash`` — writes one ``ComplianceAuditChain``
  row for a closed day. The hash chains forward (sha256 of prior day's
  hash + sorted event content). Run by the daily worker.

ULID generation: we use ``secrets.token_hex`` to keep this dependency-
light; the ULID-shape audit_id is for human readability + sort, not for
cryptographic ordering.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func

from app.models.db import (
    ComplianceEvent,
    CompliancePolicyChange,
    ComplianceAuditChain,
)


def generate_audit_id() -> str:
    """ULID-shape audit ID: ``comp_<10-char-time><6-char-rand>``.
    Not strictly ULID but sort-friendly + globally unique enough for
    audit purposes.
    """
    ts = int(datetime.utcnow().timestamp() * 1000)
    return f"comp_{ts:013x}{secrets.token_hex(6)}"


def generate_policy_change_id() -> str:
    """ULID-shape policy change ID: ``ppc_<14-char>``."""
    ts = int(datetime.utcnow().timestamp() * 1000)
    return f"ppc_{ts:013x}{secrets.token_hex(6)}"


async def emit_event(
    db,
    *,
    audit_id: str,
    api_key_id: str,
    event_type: str,
    reason_code: str,
    http_status: int,
    requested_model: Optional[str] = None,
    served_model: Optional[str] = None,
    served_provider_id: Optional[str] = None,
    blocked_company: Optional[str] = None,
    client_user_agent: Optional[str] = None,
    matched_pattern: Optional[str] = None,
    client_identity: Optional[Dict[str, Any]] = None,
    policy_active_since: Optional[datetime] = None,
    commit: bool = False,
) -> ComplianceEvent:
    """Write one compliance_events row.

    Truncates ``client_user_agent`` to 200 chars (audit limit). Does NOT
    commit by default — the calling dispatch path commits its own
    transaction. Set ``commit=True`` for error paths that need an
    immediate flush (e.g. 451/503 raise before the request handler
    would commit).
    """
    row = ComplianceEvent(
        audit_id=audit_id,
        api_key_id=api_key_id,
        event_type=event_type,
        requested_at=datetime.utcnow(),
        requested_model=requested_model,
        served_model=served_model,
        served_provider_id=served_provider_id,
        blocked_company=blocked_company,
        reason_code=reason_code,
        client_user_agent=(client_user_agent or "")[:200] if client_user_agent else None,
        http_status=http_status,
        matched_pattern=matched_pattern,
        client_identity=json.dumps(client_identity) if client_identity else None,
        policy_active_since=policy_active_since,
    )
    db.add(row)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return row


async def emit_policy_change(
    db,
    *,
    scope: str,
    target_id: Optional[str],
    before: Dict[str, Any],
    after: Dict[str, Any],
    reason: str,
    changed_by_user_id: Optional[str],
    applied_to_peers: List[Dict[str, Any]],
    pending_peers: List[Dict[str, Any]],
    commit: bool = False,
) -> str:
    """Write one compliance_policy_changes row.

    Returns the generated ``policy_change_id`` so callers can include it
    in their response.

    ``reason`` is required (decision 6). The handler that invokes this
    must have already validated that the operator supplied a non-empty
    reason.
    """
    pcid = generate_policy_change_id()
    status = (
        "fully-acked"
        if not pending_peers
        else f"quorum-reached-{len(pending_peers)}-pending"
    )
    row = CompliancePolicyChange(
        policy_change_id=pcid,
        changed_by_user_id=changed_by_user_id,
        scope=scope,
        target_id=target_id,
        before_state=json.dumps(before, default=str),
        after_state=json.dumps(after, default=str),
        reason=reason,
        applied_to_peers=json.dumps(applied_to_peers, default=str),
        pending_peers=json.dumps(pending_peers, default=str) if pending_peers else None,
        cluster_sync_status=status,
    )
    db.add(row)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return pcid


async def compute_daily_integrity_hash(db, day: date) -> str:
    """Compute + persist the integrity hash for one closed day.

    Idempotent — if a row for the day already exists, returns its hash.
    Run by the daily worker after midnight UTC for the prior day.

    Chain construction (decision 10):
      content_per_event = f"{id}|{audit_id}|{api_key_id}|{event_type}|{http_status}"
      chain_input = prior_day_chain_hash + "".join(sorted content_per_event by id)
      chain_hash = sha256(chain_input).hexdigest()
    """
    day_iso = day.isoformat()
    existing = await db.execute(
        select(ComplianceAuditChain).where(ComplianceAuditChain.day == day_iso)
    )
    existing_row = existing.scalar_one_or_none()
    if existing_row:
        return existing_row.chain_hash

    prior_day = day - timedelta(days=1)
    prior = await db.execute(
        select(ComplianceAuditChain).where(
            ComplianceAuditChain.day == prior_day.isoformat()
        )
    )
    prior_row = prior.scalar_one_or_none()
    prior_hash = prior_row.chain_hash if prior_row else ""

    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    events = await db.execute(
        select(ComplianceEvent).where(
            ComplianceEvent.created_at >= day_start,
            ComplianceEvent.created_at < day_end,
        ).order_by(ComplianceEvent.id)
    )
    rows = events.scalars().all()
    content = "".join(
        f"{r.id}|{r.audit_id}|{r.api_key_id}|{r.event_type}|{r.http_status}"
        for r in rows
    )
    chain_hash = hashlib.sha256((prior_hash + content).encode("utf-8")).hexdigest()

    chain_row = ComplianceAuditChain(
        day=day_iso,
        row_count=len(rows),
        prior_day_chain_hash=prior_hash or None,
        chain_hash=chain_hash,
        computed_at=datetime.utcnow(),
    )
    db.add(chain_row)
    await db.commit()
    return chain_hash


async def purge_expired_events(db, retention_days: int) -> int:
    """Delete compliance_events older than ``retention_days`` (decision 7,
    default 2555 = 7 years). Returns number of rows deleted.

    Daily-worker call. Policy changes + chain rows are retained
    indefinitely (tiny tables; the audit story benefits from the full
    history).
    """
    if retention_days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    from sqlalchemy import delete
    result = await db.execute(
        delete(ComplianceEvent).where(ComplianceEvent.created_at < cutoff)
    )
    await db.commit()
    return result.rowcount or 0


__all__ = [
    "generate_audit_id",
    "generate_policy_change_id",
    "emit_event",
    "emit_policy_change",
    "compute_daily_integrity_hash",
    "purge_expired_events",
]
