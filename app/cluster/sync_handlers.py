"""v3.9.8 (P5 refactor) — per-table cluster-sync handlers extracted from
``app/cluster/sync.py`` to keep that file under 1000 lines.

Each ``_apply_<table>`` function takes an AsyncSession + a list of
dicts (the peer's payload for that table) and applies the merge:
insert-if-missing, update-if-newer, tombstone-aware. See the original
``apply_sync()`` orchestrator in ``sync.py`` for the call order.

This module is import-only — ``sync.py`` re-imports these names so
existing call sites continue to work. No behavior change vs the inline
versions; this is structural cleanup only.
"""
from __future__ import annotations

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
