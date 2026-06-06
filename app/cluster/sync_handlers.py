"""v3.9.8 (P5 refactor) — per-table cluster-sync handlers extracted from
``app/cluster/sync.py`` to keep that file under 1000 lines.

Each ``_apply_<table>`` function takes an AsyncSession + a list of
dicts (the peer's payload for that table) and applies the merge:
insert-if-missing, update-if-newer, tombstone-aware. See the original
``apply_sync()`` orchestrator in ``sync.py`` for the call order.

This module is import-only — ``sync.py`` re-imports these names so
existing call sites continue to work. No behavior change vs the inline
versions; this is structural cleanup only.

v5.0.0 also adds the two compliance audit-trail handlers
(``_apply_compliance_events`` / ``_apply_compliance_policy_changes``)
and their matching ``serialize_*`` helpers used by
``manager._build_sync_payload``.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

logger = logging.getLogger(__name__)


def _parse_iso_or_none(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


async def _apply_blocked_ips(db: AsyncSession, rows: list[dict]) -> bool:
    """v3.7.15 — merge incoming blocked_ips. Returns True if any
    row was inserted, updated, or tombstoned (so the caller can
    invalidate the ip_block middleware cache).

    LWW semantics:
      - peer.deleted_at non-null + > local.deleted_at → propagate tombstone
      - peer.added_at > local.added_at AND peer.deleted_at is null →
        re-arm (clears local tombstone if any)
      - otherwise no-op
    """
    from app.models.db import BlockedIp
    changed = False
    for r in rows:
        ip = (r.get("ip") or "").strip()
        if not ip:
            continue
        peer_added_at = _parse_iso_or_none(r.get("added_at"))
        peer_deleted_at = _parse_iso_or_none(r.get("deleted_at"))
        # v4.4.24 (BUG-080) — .limit(1) so a duplicate row can never raise
        # MultipleResultsFound and abort the whole apply_sync transaction.
        # See BUG-079: a single duplicate in provider_ai_review silently
        # broke cluster sync for ~6 days. Same defensive pattern the
        # external-usage + node-auth-state handlers already used.
        result = await db.execute(
            select(BlockedIp).where(BlockedIp.ip == ip).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            db.add(BlockedIp(
                ip=ip,
                reason=r.get("reason"),
                added_by=r.get("added_by"),
                added_at=peer_added_at,
                deleted_at=peer_deleted_at,
            ))
            # Only counts as "changed cache state" if the row is live
            if peer_deleted_at is None:
                changed = True
        else:
            # Tombstone propagation: peer says deleted, local doesn't
            # know yet (or is older). Adopt the tombstone.
            if peer_deleted_at and (
                existing.deleted_at is None or peer_deleted_at > existing.deleted_at
            ):
                existing.deleted_at = peer_deleted_at
                changed = True
            # Re-arm: peer's added_at is newer than ours AND peer has
            # no tombstone — clear local tombstone, update fields.
            elif peer_added_at and peer_deleted_at is None and (
                existing.added_at is None or peer_added_at > existing.added_at
            ):
                if existing.deleted_at is not None:
                    existing.deleted_at = None
                    changed = True
                if existing.reason != r.get("reason"):
                    existing.reason = r.get("reason")
                    changed = True
                if existing.added_by != r.get("added_by"):
                    existing.added_by = r.get("added_by")
                    changed = True
    return changed


async def _apply_ai_reviews(db: AsyncSession, rows: list[dict]) -> None:
    """v3.7.15 — merge incoming api_key_ai_review rows. PK is
    auto-increment integer per node, so we de-dup by
    (api_key_id, captured_at). LWW on the lifecycle fields
    (applied_at / reverted_at / dismissed_at)."""
    from app.models.db import ApiKeyAiReview
    for r in rows:
        api_key_id = r.get("api_key_id")
        captured_at = _parse_iso_or_none(r.get("captured_at"))
        if not (api_key_id and captured_at):
            continue
        existing = (await db.execute(
            select(ApiKeyAiReview)
            .where(ApiKeyAiReview.api_key_id == api_key_id)
            .where(ApiKeyAiReview.captured_at == captured_at)
            .limit(1)  # v4.4.24 (BUG-080) — guard against duplicate rows
        )).scalar_one_or_none()
        if existing is None:
            db.add(ApiKeyAiReview(
                api_key_id=api_key_id,
                captured_at=captured_at,
                llm_model=r.get("llm_model"),
                llm_verdict=r.get("llm_verdict") or "watch",
                llm_reasoning=r.get("llm_reasoning"),
                suggested_action=r.get("suggested_action") or "none",
                stats_summary=r.get("stats_summary"),
                applied_at=_parse_iso_or_none(r.get("applied_at")),
                applied_action=r.get("applied_action"),
                prior_rate_limit_rpm=r.get("prior_rate_limit_rpm"),
                reverted_at=_parse_iso_or_none(r.get("reverted_at")),
                dismissed_at=_parse_iso_or_none(r.get("dismissed_at")),
                suggested_block_ip=r.get("suggested_block_ip"),
            ))
        else:
            # Lifecycle transitions are monotone (None → set), so we
            # accept any non-None peer value the local row doesn't have.
            for field in ("applied_at", "reverted_at", "dismissed_at"):
                peer_val = _parse_iso_or_none(r.get(field))
                if peer_val and getattr(existing, field) is None:
                    setattr(existing, field, peer_val)
            if r.get("applied_action") and not existing.applied_action:
                existing.applied_action = r["applied_action"]


async def _apply_provider_ai_reviews(db: AsyncSession, rows: list[dict]) -> None:
    """v3.7.31 (#252 phase 4) — merge incoming provider_ai_review rows.
    Same pattern as _apply_ai_reviews for api_key_ai_review: PK is
    auto-increment integer per node, so we de-dup by (provider_id,
    captured_at). LWW on the monotone lifecycle fields (applied_at /
    reverted_at / dismissed_at)."""
    from app.models.db import ProviderAiReview
    for r in rows:
        provider_id = r.get("provider_id")
        captured_at = _parse_iso_or_none(r.get("captured_at"))
        if not (provider_id and captured_at):
            continue
        existing = (await db.execute(
            select(ProviderAiReview)
            .where(ProviderAiReview.provider_id == provider_id)
            .where(ProviderAiReview.captured_at == captured_at)
            .limit(1)  # v4.4.24 (BUG-079) — the row this crash was found on
        )).scalar_one_or_none()
        if existing is None:
            db.add(ProviderAiReview(
                provider_id=provider_id,
                captured_at=captured_at,
                llm_model=r.get("llm_model"),
                llm_verdict=r.get("llm_verdict") or "watch",
                llm_reasoning=r.get("llm_reasoning"),
                suggested_priority_delta=r.get("suggested_priority_delta"),
                suggested_auto_skip_hours=r.get("suggested_auto_skip_hours"),
                stats_summary=r.get("stats_summary"),
                applied_at=_parse_iso_or_none(r.get("applied_at")),
                applied_action=r.get("applied_action"),
                prior_priority=r.get("prior_priority"),
                prior_auto_skip_until=_parse_iso_or_none(r.get("prior_auto_skip_until")),
                reverted_at=_parse_iso_or_none(r.get("reverted_at")),
                dismissed_at=_parse_iso_or_none(r.get("dismissed_at")),
            ))
        else:
            for field in ("applied_at", "reverted_at", "dismissed_at"):
                peer_val = _parse_iso_or_none(r.get(field))
                if peer_val and getattr(existing, field) is None:
                    setattr(existing, field, peer_val)
            if r.get("applied_action") and not existing.applied_action:
                existing.applied_action = r["applied_action"]


async def _apply_caller_memory(db: AsyncSession, rows: list[dict]) -> None:
    """v3.8.7 (#267) Phase 2 — merge incoming caller_memory rows.

    Dedup by (api_key_id, conversation_id, memory_tag). LWW by
    updated_at. Tombstones (deleted_at non-null) propagate so a
    DELETE on one node reaches peers.

    NOTE: this only manages the SQLite king-store. Redis cache
    invalidation happens at the call site (Phase 3 ship) — apply_sync
    must be cheap and not require Redis to be up.
    """
    from app.models.db import CallerMemory
    for r in rows:
        akid = r.get("api_key_id")
        conv = r.get("conversation_id")  # nullable
        tag = r.get("memory_tag") or "default"
        if not akid:
            continue
        peer_ts = r.get("updated_at") or 0
        existing = (await db.execute(
            select(CallerMemory)
            .where(CallerMemory.api_key_id == akid)
            .where(CallerMemory.conversation_id.is_(None) if conv is None else CallerMemory.conversation_id == conv)
            .where(CallerMemory.memory_tag == tag)
            .limit(1)  # v4.4.24 (BUG-080) — guard against duplicate rows
        )).scalar_one_or_none()
        if existing is None:
            db.add(CallerMemory(
                api_key_id=akid,
                conversation_id=conv,
                memory_tag=tag,
                content=r.get("content") or "",
                content_format=r.get("content_format") or "text",
                updated_at=peer_ts,
                updated_by_node=r.get("updated_by_node"),
                source_provider_id=r.get("source_provider_id"),
                source_request_id=r.get("source_request_id"),
                deleted_at=r.get("deleted_at"),
            ))
        else:
            # LWW: only adopt the peer's view when its timestamp is
            # strictly newer (== keeps local stamps stable on tie).
            if peer_ts > (existing.updated_at or 0):
                existing.content = r.get("content") or ""
                existing.content_format = r.get("content_format") or "text"
                existing.updated_at = peer_ts
                existing.updated_by_node = r.get("updated_by_node")
                existing.source_provider_id = r.get("source_provider_id")
                existing.source_request_id = r.get("source_request_id")
                existing.deleted_at = r.get("deleted_at")


async def _apply_caller_memory_markers(db: AsyncSession, rows: list[dict]) -> None:
    """v3.8.7 (#267) Phase 2 — merge incoming caller_memory_marker
    rows. Markers are monotonically-extending records:
    first_seen_at never decreases, last_known_* updates with each
    cross-provider write, recovered_at lifecycle transitions
    None→set.

    Dedup by (api_key_id, conversation_id, memory_tag).
    """
    from app.models.db import CallerMemoryMarker
    for r in rows:
        akid = r.get("api_key_id")
        conv = r.get("conversation_id")
        tag = r.get("memory_tag") or "default"
        if not akid:
            continue
        existing = (await db.execute(
            select(CallerMemoryMarker)
            .where(CallerMemoryMarker.api_key_id == akid)
            .where(CallerMemoryMarker.conversation_id.is_(None) if conv is None else CallerMemoryMarker.conversation_id == conv)
            .where(CallerMemoryMarker.memory_tag == tag)
            .limit(1)  # v4.4.24 (BUG-080) — guard against duplicate rows
        )).scalar_one_or_none()
        if existing is None:
            db.add(CallerMemoryMarker(
                api_key_id=akid,
                conversation_id=conv,
                memory_tag=tag,
                first_seen_at=r.get("first_seen_at") or 0,
                last_known_provider_id=r.get("last_known_provider_id"),
                last_known_external_ref=r.get("last_known_external_ref"),
                recovered_at=r.get("recovered_at"),
                deleted_at=r.get("deleted_at"),
            ))
        else:
            # first_seen_at is the EARLIEST — keep the min so backups
            # that restore older markers don't accidentally shift it
            # forward.
            peer_first = r.get("first_seen_at") or 0
            if peer_first and (existing.first_seen_at is None or peer_first < existing.first_seen_at):
                existing.first_seen_at = peer_first
            # last_known_* picks the most recent write — but we don't
            # have a timestamp for those; treat any non-null peer value
            # as more authoritative than a local null.
            if r.get("last_known_provider_id") and not existing.last_known_provider_id:
                existing.last_known_provider_id = r["last_known_provider_id"]
            if r.get("last_known_external_ref") and not existing.last_known_external_ref:
                existing.last_known_external_ref = r["last_known_external_ref"]
            # recovered_at is monotone (None→set, never reverts)
            peer_rec = r.get("recovered_at")
            if peer_rec and not existing.recovered_at:
                existing.recovered_at = peer_rec


async def _apply_provider_node_auth_states(db: AsyncSession, rows: list[dict]) -> None:
    """v4.4 M-2 — merge incoming ``provider_node_auth_state`` rows.

    PK is composite ``(provider_id, node_id)``. Each node OWNS its
    own rows (the row for ``node_id == settings.cluster_node_id``)
    and writes them locally; cluster sync propagates them to peers
    for cluster-wide visibility (admin UI + downstream tooling).

    LWW conflict resolution: prefer the row with the most recent
    ``last_check_at``. If a peer's row arrives with a newer
    timestamp than what we have, we accept it. If our local row is
    newer, we keep it. A peer should NEVER be writing for OUR
    node_id, but if it happens (clock skew, accidental write), the
    LWW comparison still favours the newest observation.
    """
    from app.models.db import ProviderNodeAuthState
    for r in rows:
        provider_id = r.get("provider_id")
        node_id = r.get("node_id")
        if not (provider_id and node_id):
            continue
        last_check_at = _parse_iso_or_none(r.get("last_check_at"))
        existing = (await db.execute(
            select(ProviderNodeAuthState)
            .where(ProviderNodeAuthState.provider_id == provider_id)
            .where(ProviderNodeAuthState.node_id == node_id)
            .limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            # LWW: skip if our local row is newer than the incoming one.
            if (
                existing.last_check_at
                and last_check_at
                and existing.last_check_at >= last_check_at
            ):
                continue
            existing.auth_state = r.get("auth_state") or "never_authed"
            existing.last_ok_at = _parse_iso_or_none(r.get("last_ok_at"))
            existing.last_check_at = last_check_at
            existing.reauth_url = r.get("reauth_url")
            existing.last_error = (r.get("last_error") or None)
            continue
        db.add(ProviderNodeAuthState(
            provider_id=provider_id,
            node_id=node_id,
            auth_state=r.get("auth_state") or "never_authed",
            last_ok_at=_parse_iso_or_none(r.get("last_ok_at")),
            last_check_at=last_check_at,
            reauth_url=r.get("reauth_url"),
            last_error=r.get("last_error"),
        ))


async def _apply_external_usage_snapshots(db: AsyncSession, rows: list[dict]) -> None:
    """v3.7.15 — merge incoming external_usage_snapshot rows. Each row
    represents one provider's latest snapshot at peer-capture-time.
    Insert if no row exists at that exact captured_at; append-only
    (we never overwrite history)."""
    from app.models.db import ExternalUsageSnapshot
    for r in rows:
        provider_id = r.get("provider_id")
        captured_at = _parse_iso_or_none(r.get("captured_at"))
        if not (provider_id and captured_at):
            continue
        existing = (await db.execute(
            select(ExternalUsageSnapshot)
            .where(ExternalUsageSnapshot.provider_id == provider_id)
            .where(ExternalUsageSnapshot.captured_at == captured_at)
            .limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(ExternalUsageSnapshot(
            provider_id=provider_id,
            captured_at=captured_at,
            source=r.get("source") or "anthropic_console_v1",
            http_status=r.get("http_status"),
            error=r.get("error"),
            auth_state=r.get("auth_state"),
            five_hour_utilization=r.get("five_hour_utilization"),
            five_hour_resets_at=_parse_iso_or_none(r.get("five_hour_resets_at")),
            seven_day_utilization=r.get("seven_day_utilization"),
            seven_day_resets_at=_parse_iso_or_none(r.get("seven_day_resets_at")),
            seven_day_sonnet_utilization=r.get("seven_day_sonnet_utilization"),
            seven_day_sonnet_resets_at=_parse_iso_or_none(r.get("seven_day_sonnet_resets_at")),
            seven_day_opus_utilization=r.get("seven_day_opus_utilization"),
            seven_day_opus_resets_at=_parse_iso_or_none(r.get("seven_day_opus_resets_at")),
            extra_usage_is_enabled=r.get("extra_usage_is_enabled"),
            extra_usage_monthly_limit=r.get("extra_usage_monthly_limit"),
            extra_usage_used_credits=r.get("extra_usage_used_credits"),
            extra_usage_utilization=r.get("extra_usage_utilization"),
            extra_usage_currency=r.get("extra_usage_currency"),
        ))


# ───────────────────────────────────────────────────────────────────
# v5.0.0 — compliance audit-trail handlers + serialization helpers.
# ───────────────────────────────────────────────────────────────────


def _iso_or_none(v):
    """ISO-encode a datetime or None — used for outbound serialization."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def serialize_compliance_event(r) -> dict:
    """Render a ``ComplianceEvent`` row in the wire shape consumed by
    ``_apply_compliance_events``. Dict keys mirror the column names so
    the apply handler can pass the payload straight through to the
    constructor (after filtering to valid columns)."""
    return {
        "audit_id": r.audit_id,
        "api_key_id": r.api_key_id,
        "event_type": r.event_type,
        "requested_at": _iso_or_none(r.requested_at),
        "requested_model": r.requested_model,
        "served_model": r.served_model,
        "served_provider_id": r.served_provider_id,
        "blocked_company": r.blocked_company,
        "reason_code": r.reason_code,
        "client_user_agent": r.client_user_agent,
        "http_status": r.http_status,
        "matched_pattern": r.matched_pattern,
        "client_identity": r.client_identity,
        "policy_active_since": _iso_or_none(r.policy_active_since),
        "created_at": _iso_or_none(r.created_at),
    }


def serialize_policy_change(r) -> dict:
    """Render a ``CompliancePolicyChange`` row in the wire shape consumed
    by ``_apply_compliance_policy_changes``."""
    return {
        "policy_change_id": r.policy_change_id,
        "changed_at": _iso_or_none(r.changed_at),
        "changed_by_user_id": r.changed_by_user_id,
        "scope": r.scope,
        "target_id": r.target_id,
        "before_state": r.before_state,
        "after_state": r.after_state,
        "reason": r.reason,
        "applied_to_peers": r.applied_to_peers,
        "pending_peers": r.pending_peers,
        "cluster_sync_status": r.cluster_sync_status,
    }


# Datetime columns on the compliance tables — converted on the way in
# so a payload built with ``serialize_*`` round-trips losslessly.
_COMPLIANCE_EVENT_DT_COLS = {
    "requested_at", "policy_active_since", "created_at",
}
_POLICY_CHANGE_DT_COLS = {"changed_at"}


def _row_to_kwargs(row: dict, allowed: set[str], dt_cols: set[str]) -> dict:
    """Filter incoming wire-row to model columns + parse ISO datetime
    strings back to ``datetime``. Skips unknown keys defensively in case
    a future schema bump adds a field the local build doesn't know about."""
    out = {}
    for k, v in row.items():
        if k not in allowed:
            continue
        if k in dt_cols and isinstance(v, str):
            v = _parse_iso_or_none(v)
        out[k] = v
    return out


async def _apply_compliance_events(db: AsyncSession, rows: list[dict]) -> int:
    """v5.0.0 — append-only merge of ``compliance_events`` rows.

    Dedup on the unique business key ``audit_id`` (ULID-shape, per spec
    §3.2). Mirrors ``_apply_blocked_ips``'s ``.limit(1)`` guard so a
    duplicate row from a misbehaving peer can never raise
    ``MultipleResultsFound`` and abort the whole apply_sync transaction
    (BUG-079/BUG-080 discipline).

    Returns the number of rows actually inserted (helpful for tests +
    logging)."""
    from app.models.db import ComplianceEvent
    applied = 0
    allowed_cols = {col.name for col in ComplianceEvent.__table__.columns}
    for row in rows:
        audit_id = row.get("audit_id")
        if not audit_id:
            continue
        existing = await db.execute(
            select(ComplianceEvent)
            .where(ComplianceEvent.audit_id == audit_id)
            .limit(1)  # BUG-080 guard
        )
        if existing.scalar_one_or_none():
            continue
        kwargs = _row_to_kwargs(row, allowed_cols, _COMPLIANCE_EVENT_DT_COLS)
        db.add(ComplianceEvent(**kwargs))
        applied += 1
    if applied:
        await db.commit()
    return applied


async def _apply_compliance_policy_changes(
    db: AsyncSession, rows: list[dict]
) -> int:
    """v5.0.0 — append-only merge of ``compliance_policy_changes`` rows.

    Dedup on ``policy_change_id``. Same shape as
    ``_apply_compliance_events``."""
    from app.models.db import CompliancePolicyChange
    applied = 0
    allowed_cols = {col.name for col in CompliancePolicyChange.__table__.columns}
    for row in rows:
        policy_change_id = row.get("policy_change_id")
        if not policy_change_id:
            continue
        existing = await db.execute(
            select(CompliancePolicyChange)
            .where(CompliancePolicyChange.policy_change_id == policy_change_id)
            .limit(1)  # BUG-080 guard
        )
        if existing.scalar_one_or_none():
            continue
        kwargs = _row_to_kwargs(row, allowed_cols, _POLICY_CHANGE_DT_COLS)
        db.add(CompliancePolicyChange(**kwargs))
        applied += 1
    if applied:
        await db.commit()
    return applied


def _parse_iso_naive_utc(v):
    """Parse a peer-side ISO timestamp into a NAIVE UTC datetime.

    SQLAlchemy's ``Column(DateTime)`` without ``timezone=True`` returns
    naive values when reading from SQLite, so we strip tzinfo here to
    keep comparisons consistent across LWW branches. Without this, peer
    payloads (ISO strings with explicit offsets) compare as tz-aware
    against locally-loaded naive datetimes and TypeError out on
    ``>=`` / ``>`` ops.
    """
    from datetime import datetime, timezone
    if not v:
        return None
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(v, datetime):
        dt = v
    else:
        return v
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_iso_keep_naive(v):
    """Like ``_parse_iso_naive_utc`` but accepts both ``datetime`` and
    ``str`` without further normalization — used for the api_keys
    ``deleted_at`` field which is a plain datetime column.

    v5.0.25 / Batch 4 (BUG-064) — returns ``None`` on any unrecognized
    type (int, float, dict, list, …) and logs a debug breadcrumb,
    rather than silently returning the raw value. Pre-fix, a peer
    pushing ``deleted_at: 1780777200`` (Unix timestamp) would get
    that int stored as-is, and the next LWW comparison against a
    real ``datetime`` would raise TypeError and crash the section.
    """
    from datetime import datetime as _dt
    if v is None or v == "":
        return None
    if isinstance(v, _dt):
        return v
    if isinstance(v, str):
        try:
            return _dt.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    # Unrecognized type — defensive fallback so a malformed peer
    # payload skips the field rather than poisoning the sync section.
    import logging as _lg
    _lg.getLogger(__name__).warning(
        "_parse_iso_keep_naive: unrecognized type=%s value=%r — "
        "returning None (pre-v5.0.25 would have stored as-is and "
        "crashed the next LWW comparison).",
        type(v).__name__, str(v)[:80],
    )
    return None


async def _apply_api_keys(
    db: AsyncSession, rows: list[dict]
) -> dict[str, float]:
    """v5.0.10 — extracted from ``apply_sync``. Merge incoming api_keys.

    Returns the ``peer_costs`` dict (key_id → total_cost_usd reported by
    the peer for this row) that the caller folds into the per-peer
    cost-tracking map ``_peer_key_costs[source_node]``.

    The merge semantics are unchanged from the original inline block:

    - **Tombstone-aware** (v3.0.20): a peer's ``deleted_at`` propagates
      locally when we have no tombstone OR peer's stamp is newer; a
      local tombstone outranks any non-tombstoned peer payload.
    - **LWW gate** (v4.4.20): per-row ``last_user_edit_at`` decides
      accept/reject. Tie → keep local (anti-ping-pong); peer-stamped +
      local-unstamped → accept (legacy upgrade path); neither stamped
      → accept (pre-LWW legacy behavior).
    - **Membership-test field coverage** (v4.4.18): every
      operator-settable field uses ``if "field" in k_data:`` so a peer
      omitting the field (older build) doesn't clobber the local value
      with None. v5.0.0 added the three compliance columns
      (``blocked_companies`` / ``allowed_paths`` /
      ``debug_echo_enabled``) under the same discipline.
    - **Compliance cache invalidation** (v5.0.0): when
      ``blocked_companies`` changes via sync, the per-key blocklist
      cache is invalidated so the next request sees the new policy
      without waiting up to 30s for TTL.
    - **Full-field INSERT** (v4.4.25 / BUG-084): on first
      materialization we set every operator-settable field, not just
      the base columns, so a freshly-created + immediately-PATCHed key
      doesn't get stuck at defaults under the LWW tie on the next sync
      round-trip.
    """
    from app.models.db import ApiKey
    peer_costs: dict[str, float] = {}
    for k_data in rows:
        peer_deleted_at = _parse_iso_keep_naive(k_data.get("deleted_at"))
        peer_user_edit_at = k_data.get("last_user_edit_at")
        if peer_user_edit_at is not None:
            try:
                peer_user_edit_at = float(peer_user_edit_at)
            except (TypeError, ValueError):
                peer_user_edit_at = None
        result = await db.execute(
            select(ApiKey).where(ApiKey.key_hash == k_data["key_hash"]).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            if peer_deleted_at and (
                existing.deleted_at is None
                or peer_deleted_at >= existing.deleted_at
            ):
                existing.deleted_at = peer_deleted_at
                existing.enabled = False
                continue
            if existing.deleted_at is not None and peer_deleted_at is None:
                continue
            local_user_edit = existing.last_user_edit_at
            if (
                peer_user_edit_at is not None
                and local_user_edit is not None
            ):
                if peer_user_edit_at == local_user_edit:
                    accept = False
                else:
                    accept = peer_user_edit_at > local_user_edit
            elif (
                local_user_edit is not None and peer_user_edit_at is None
            ):
                accept = False
            else:
                accept = True
            if not accept:
                continue
            if "spending_cap_usd" in k_data:
                existing.spending_cap_usd = k_data["spending_cap_usd"]
            if "rate_limit_rpm" in k_data:
                existing.rate_limit_rpm = k_data["rate_limit_rpm"]
            if "enabled" in k_data:
                existing.enabled = k_data["enabled"]
            if "semantic_cache_enabled" in k_data:
                existing.semantic_cache_enabled = bool(
                    k_data["semantic_cache_enabled"]
                )
            if "daily_soft_cap_usd" in k_data:
                existing.daily_soft_cap_usd = k_data["daily_soft_cap_usd"]
            if "daily_hard_cap_usd" in k_data:
                existing.daily_hard_cap_usd = k_data["daily_hard_cap_usd"]
            if "hourly_cap_usd" in k_data:
                existing.hourly_cap_usd = k_data["hourly_cap_usd"]
            if "rate_limit_tier" in k_data:
                existing.rate_limit_tier = k_data["rate_limit_tier"]
            if "caller_memory_ttl_days" in k_data:
                existing.caller_memory_ttl_days = (
                    k_data["caller_memory_ttl_days"]
                )
            if "lmrh_polling_rpm" in k_data:
                existing.lmrh_polling_rpm = k_data["lmrh_polling_rpm"]
            if "lmrh_quotes_rpm" in k_data:
                existing.lmrh_quotes_rpm = k_data["lmrh_quotes_rpm"]
            if "blocked_companies" in k_data:
                existing.blocked_companies = k_data["blocked_companies"]
                try:
                    from app.compliance.policy import (
                        invalidate_blocklist_cache,
                    )
                    invalidate_blocklist_cache(existing.id)
                except Exception:
                    pass
            if "allowed_paths" in k_data:
                existing.allowed_paths = k_data["allowed_paths"]
            if "debug_echo_enabled" in k_data:
                existing.debug_echo_enabled = bool(
                    k_data["debug_echo_enabled"]
                )
            if peer_user_edit_at is not None:
                existing.last_user_edit_at = peer_user_edit_at
        else:
            if peer_deleted_at is not None:
                continue
            db.add(ApiKey(
                id=k_data["id"],
                name=k_data["name"],
                key_hash=k_data["key_hash"],
                key_prefix=k_data["key_prefix"],
                key_type=k_data.get("key_type", "standard"),
                enabled=k_data.get("enabled", True),
                spending_cap_usd=k_data.get("spending_cap_usd"),
                rate_limit_rpm=k_data.get("rate_limit_rpm"),
                semantic_cache_enabled=bool(
                    k_data.get("semantic_cache_enabled", False)
                ),
                daily_soft_cap_usd=k_data.get("daily_soft_cap_usd"),
                daily_hard_cap_usd=k_data.get("daily_hard_cap_usd"),
                hourly_cap_usd=k_data.get("hourly_cap_usd"),
                rate_limit_tier=k_data.get("rate_limit_tier"),
                caller_memory_ttl_days=k_data.get("caller_memory_ttl_days"),
                lmrh_polling_rpm=k_data.get("lmrh_polling_rpm"),
                lmrh_quotes_rpm=k_data.get("lmrh_quotes_rpm"),
                blocked_companies=k_data.get("blocked_companies"),
                allowed_paths=k_data.get("allowed_paths"),
                debug_echo_enabled=bool(
                    k_data.get("debug_echo_enabled", False)
                ),
                last_user_edit_at=peer_user_edit_at,
            ))
        key_id = k_data.get("id")
        if key_id and "total_cost_usd" in k_data:
            peer_costs[key_id] = float(k_data["total_cost_usd"])
    return peer_costs


async def _apply_providers(db: AsyncSession, rows: list[dict]) -> None:
    """v5.0.10 — extracted from ``apply_sync``. Merge incoming providers.

    Behavior identical to the original inline block:

    - **Match by id, fallback to name** for legacy pre-v2.8.2 rows that
      may have different ids on each node.
    - **Tombstone-aware** (v2.8.2 / v4.4.2 / BUG-053): peer-tombstone
      propagates whenever we don't have a tombstone (the v2.8.2 gate
      ``peer_deleted_at >= local_updated`` failed when background
      activity bumped local.updated_at past the originator's
      tombstone). Clears local CB state on inbound tombstone
      propagation (v3.5.9 BUG-012).
    - **LWW gate** (v3.0.11): per-row ``last_user_edit_at`` decides
      accept/reject. v3.0.63 strict-greater on ties (anti-ping-pong).
      v3.2.7 fall-through to legacy ``updated_at`` LWW when both stamps
      tie (catches background mutations that didn't bump
      last_user_edit_at).
    - **Membership-test field coverage** for all operator-settable
      fields including v3.7.3 Anthropic billing,
      v3.7.27 Codex billing, v3.7.28 manual override, and v5.0.0
      owner_company. Cookies (anthropic_session_cookies /
      codex_session_cookies) are INTENTIONALLY not synced — auth
      material stays on the capture node.
    - **owner_company change** (v5.0.0): clears the GLOBAL blocklist
      cache (not just per-key) because the router reads
      ``provider.owner_company`` at request time and that affects every
      key's effective filter.
    - **CB state init**: calls ``register_provider`` on freshly inserted
      rows so the circuit-breaker state is set up immediately.
    """
    from app.models.db import Provider
    from app.monitoring.status import register_provider
    for p_data in rows:
        peer_deleted_at = _parse_iso_naive_utc(p_data.get("deleted_at"))
        peer_updated_at = _parse_iso_naive_utc(p_data.get("updated_at"))
        peer_user_edit_at = p_data.get("last_user_edit_at")
        if peer_user_edit_at is not None:
            try:
                peer_user_edit_at = float(peer_user_edit_at)
            except (TypeError, ValueError):
                peer_user_edit_at = None

        result = await db.execute(
            select(Provider).where(Provider.id == p_data["id"]).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            result2 = await db.execute(
                select(Provider).where(Provider.name == p_data["name"]).limit(1)
            )
            existing = result2.scalar_one_or_none()

        if existing is not None:
            local_updated = existing.updated_at
            local_deleted = existing.deleted_at

            if peer_deleted_at and not local_deleted:
                existing.deleted_at = peer_deleted_at
                existing.enabled = False
                if peer_updated_at and (
                    local_updated is None or peer_updated_at > local_updated
                ):
                    existing.updated_at = peer_updated_at
                try:
                    from app.routing.circuit_breaker import (
                        _local_states as _cb_states,
                        _auth_failed as _cb_auth_failed,
                    )
                    _cb_states.pop(existing.id, None)
                    _cb_auth_failed.pop(existing.id, None)
                except Exception:
                    pass
                continue

            if local_deleted and (
                peer_updated_at is None or local_deleted >= peer_updated_at
            ):
                continue

            local_user_edit = existing.last_user_edit_at
            if (
                peer_user_edit_at is not None
                and local_user_edit is not None
            ):
                if peer_user_edit_at != local_user_edit:
                    accept = peer_user_edit_at > local_user_edit
                else:
                    accept = (
                        peer_updated_at is not None
                        and local_updated is not None
                        and peer_updated_at > local_updated
                    )
            elif (
                local_user_edit is not None and peer_user_edit_at is None
            ):
                accept = False
            else:
                accept = (
                    peer_updated_at is None
                    or local_updated is None
                    or peer_updated_at >= local_updated
                )
            if accept:
                if "name" in p_data:
                    existing.name = p_data["name"]
                if "provider_type" in p_data:
                    existing.provider_type = p_data["provider_type"]
                existing.api_key = p_data.get("api_key", existing.api_key)
                existing.base_url = p_data.get("base_url", existing.base_url)
                existing.default_model = p_data.get(
                    "default_model", existing.default_model
                )
                existing.priority = p_data.get(
                    "priority", existing.priority
                )
                existing.enabled = p_data.get("enabled", existing.enabled)
                existing.timeout_sec = p_data.get(
                    "timeout_sec", existing.timeout_sec
                )
                existing.exclude_from_tool_requests = p_data.get(
                    "exclude_from_tool_requests",
                    existing.exclude_from_tool_requests,
                )
                existing.hold_down_sec = p_data.get(
                    "hold_down_sec", existing.hold_down_sec
                )
                existing.failure_threshold = p_data.get(
                    "failure_threshold", existing.failure_threshold
                )
                existing.extra_config = p_data.get(
                    "extra_config", existing.extra_config
                )
                if "owned_by_key_id" in p_data:
                    existing.owned_by_key_id = p_data["owned_by_key_id"]
                if "daily_budget_usd" in p_data:
                    existing.daily_budget_usd = p_data["daily_budget_usd"]
                if "oauth_refresh_token" in p_data:
                    existing.oauth_refresh_token = p_data[
                        "oauth_refresh_token"
                    ]
                if "oauth_expires_at" in p_data:
                    existing.oauth_expires_at = p_data["oauth_expires_at"]
                if "anthropic_org_uuid" in p_data:
                    existing.anthropic_org_uuid = p_data.get(
                        "anthropic_org_uuid"
                    )
                if "anthropic_session_captured_at" in p_data:
                    existing.anthropic_session_captured_at = p_data.get(
                        "anthropic_session_captured_at"
                    )
                if "codex_usage_endpoint_url" in p_data:
                    existing.codex_usage_endpoint_url = p_data.get(
                        "codex_usage_endpoint_url"
                    )
                if "codex_session_captured_at" in p_data:
                    existing.codex_session_captured_at = p_data.get(
                        "codex_session_captured_at"
                    )
                if "manual_override_until" in p_data:
                    existing.manual_override_until = _parse_iso_or_none(
                        p_data.get("manual_override_until")
                    )
                if "manual_override_set_by" in p_data:
                    existing.manual_override_set_by = p_data.get(
                        "manual_override_set_by"
                    )
                if "manual_override_set_at" in p_data:
                    existing.manual_override_set_at = _parse_iso_or_none(
                        p_data.get("manual_override_set_at")
                    )
                if "manual_override_reason" in p_data:
                    existing.manual_override_reason = p_data.get(
                        "manual_override_reason"
                    )
                if "auto_skip_until" in p_data:
                    val = p_data.get("auto_skip_until")
                    if val:
                        from datetime import datetime as _dt
                        try:
                            existing.auto_skip_until = (
                                _dt.fromisoformat(val.replace("Z", "+00:00"))
                            )
                        except Exception:
                            existing.auto_skip_until = None
                    else:
                        existing.auto_skip_until = None
                if "auto_skip_reason" in p_data:
                    existing.auto_skip_reason = p_data.get(
                        "auto_skip_reason"
                    )
                if "owner_company" in p_data:
                    existing.owner_company = p_data.get("owner_company")
                    try:
                        from app.compliance.policy import (
                            invalidate_blocklist_cache,
                        )
                        invalidate_blocklist_cache(None)
                    except Exception:
                        pass
                if peer_updated_at:
                    existing.updated_at = peer_updated_at
                if peer_user_edit_at is not None:
                    existing.last_user_edit_at = peer_user_edit_at
            continue

        if peer_deleted_at is not None:
            continue
        p = Provider(
            id=p_data["id"],
            name=p_data["name"],
            provider_type=p_data["provider_type"],
            api_key=p_data.get("api_key"),
            base_url=p_data.get("base_url"),
            default_model=p_data.get("default_model"),
            priority=p_data.get("priority", 10),
            enabled=p_data.get("enabled", True),
            timeout_sec=p_data.get("timeout_sec", 60),
            exclude_from_tool_requests=p_data.get(
                "exclude_from_tool_requests", False
            ),
            hold_down_sec=p_data.get("hold_down_sec"),
            failure_threshold=p_data.get("failure_threshold"),
            extra_config=p_data.get("extra_config", {}),
            owned_by_key_id=p_data.get("owned_by_key_id"),
            daily_budget_usd=p_data.get("daily_budget_usd"),
            oauth_refresh_token=p_data.get("oauth_refresh_token"),
            oauth_expires_at=p_data.get("oauth_expires_at"),
            anthropic_org_uuid=p_data.get("anthropic_org_uuid"),
            anthropic_session_captured_at=p_data.get(
                "anthropic_session_captured_at"
            ),
            codex_usage_endpoint_url=p_data.get("codex_usage_endpoint_url"),
            codex_session_captured_at=p_data.get(
                "codex_session_captured_at"
            ),
            manual_override_until=_parse_iso_or_none(
                p_data.get("manual_override_until")
            ),
            manual_override_set_by=p_data.get("manual_override_set_by"),
            manual_override_set_at=_parse_iso_or_none(
                p_data.get("manual_override_set_at")
            ),
            manual_override_reason=p_data.get("manual_override_reason"),
            auto_skip_until=_parse_iso_or_none(
                p_data.get("auto_skip_until")
            ),
            auto_skip_reason=p_data.get("auto_skip_reason"),
            owner_company=p_data.get("owner_company"),
            last_user_edit_at=peer_user_edit_at,
        )
        db.add(p)
        register_provider(
            p.id, p.provider_type, p.hold_down_sec, p.failure_threshold,
        )


async def _apply_cluster_peers(db: AsyncSession, rows: list[dict]) -> None:
    """v5.0.18 — merge incoming ``cluster_peers`` rows.

    PK is the string ``id`` (the remote node's CLUSTER_NODE_ID).
    Conflict resolution:

      - **Tombstone-aware**: if the incoming row has ``removed_at`` set
        AND it's >= our local ``removed_at`` (or our local is NULL),
        we accept the deletion. Mirrors api_keys/providers tombstone
        semantics so a remove on any peer propagates everywhere.
      - **LWW on edits**: when neither side is tombstoned, the row with
        the higher ``last_user_edit_at`` wins on ``url`` + ``name``.
      - **Self-row protection**: never sync a row whose id equals this
        node's CLUSTER_NODE_ID. Each node knows itself; the
        cluster_peers table is for OTHER nodes only. If a peer
        accidentally includes our id in its payload (clock skew on a
        UI add operation), we silently skip it.
    """
    from app.config import settings as _s
    from app.models.db import ClusterPeer
    self_id = _s.cluster_node_id or ""
    for r in rows:
        pid = r.get("id")
        if not pid or pid == self_id:
            continue
        peer_removed = _parse_iso_keep_naive(r.get("removed_at"))
        peer_added = _parse_iso_keep_naive(r.get("added_at"))
        peer_edit = r.get("last_user_edit_at")
        if peer_edit is not None:
            try:
                peer_edit = float(peer_edit)
            except (TypeError, ValueError):
                peer_edit = None
        existing = (await db.execute(
            select(ClusterPeer).where(ClusterPeer.id == pid).limit(1)
        )).scalar_one_or_none()

        if existing is None:
            db.add(ClusterPeer(
                id=pid,
                url=r.get("url") or "",
                name=r.get("name"),
                added_at=peer_added,
                removed_at=peer_removed,
                last_user_edit_at=peer_edit,
            ))
            continue

        # Tombstone branch — accept a removal whose timestamp is newer
        # than ours (or whose ours is NULL, meaning we still think the
        # peer is active).
        if peer_removed and (
            existing.removed_at is None
            or peer_removed >= existing.removed_at
        ):
            existing.removed_at = peer_removed
            continue
        # Local is tombstoned, peer says active → keep local tombstone.
        if existing.removed_at is not None and peer_removed is None:
            continue
        # LWW on edits.
        local_edit = existing.last_user_edit_at
        if (
            peer_edit is not None
            and local_edit is not None
            and peer_edit <= local_edit
        ):
            continue
        if r.get("url"):
            existing.url = r["url"]
        if "name" in r:
            existing.name = r.get("name")
        if peer_edit is not None:
            existing.last_user_edit_at = peer_edit


__all__ = [
    "_apply_blocked_ips",
    "_apply_ai_reviews",
    "_apply_provider_ai_reviews",
    "_apply_caller_memory",
    "_apply_caller_memory_markers",
    "_apply_external_usage_snapshots",
    "_apply_provider_node_auth_states",
    "_apply_compliance_events",
    "_apply_compliance_policy_changes",
    "_apply_api_keys",
    "_apply_providers",
    "_apply_cluster_peers",
    "_parse_iso_naive_utc",
    "serialize_compliance_event",
    "serialize_policy_change",
]
