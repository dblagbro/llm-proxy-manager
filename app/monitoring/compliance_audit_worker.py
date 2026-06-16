"""Daily compliance audit worker (v5.0.1+).

Closes two gaps in the v5.0.0 ship:

1. **Daily integrity hash chain** (decision 10). At ~00:30 UTC the worker
   computes ``compliance_audit_chain`` for the closed prior day. The chain
   hashes forward (sha256 of prior day's chain_hash + sorted event-id +
   content fields) so tampering with any closed day's events breaks the
   chain at every subsequent day. Idempotent — calling
   ``compute_daily_integrity_hash`` for a day that already has a row is a
   no-op.

2. **Retention purge** (decision 7). Daily, deletes
   ``compliance_events`` rows older than
   ``SystemSetting.compliance_audit_retention_days`` (default 2555 =
   7 years). Policy-change rows + chain rows are NOT purged — those are
   tiny tables and the audit story benefits from the full history.

Same shape as ``app/monitoring/prune.py``: an asyncio task scheduled in
``app/main.py`` lifespan. Errors are swallowed and logged — must never
block the request loop. The last-sweep snapshot is surfaced via
``get_last_sweep()`` for the admin debug endpoint.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from app.compliance.audit import (
    compute_daily_integrity_hash,
    purge_expired_events,
)


logger = logging.getLogger(__name__)


_DEFAULT_RETENTION_DAYS = 2555
_SWEEP_INTERVAL_SEC = 24 * 60 * 60
# 90 minutes after startup — lets the prune loop fire first (1h delay), then
# we run; avoids two DB-heavy workers stampeding on boot.
_INITIAL_DELAY_SEC = 90 * 60


_LAST_SWEEP_RESULT: Dict[str, Any] = {
    "last_sweep_ts": None,
    "last_hash_day": None,
    "last_hash": None,
    "last_purge_count": 0,
    "retention_days": _DEFAULT_RETENTION_DAYS,
}


# v5.4.0 (BUG-073): how many consecutive zero-row signed days before
# the worker emits a warning to activity_log. 3 = a long weekend won't
# trigger; a Mon-Wed silent week does.
_ZERO_ROW_WARN_THRESHOLD = 3


async def _emit_zero_row_warning_if_threshold(db, just_signed_day) -> None:
    """If the last N consecutive ``compliance_audit_chain`` rows
    (including ``just_signed_day``) all have ``row_count = 0``, emit
    one ``audit_chain_zero_row_streak`` warning event to activity_log.

    Idempotent — the warning is only emitted once per streak threshold
    boundary; subsequent zero-row days don't multiply the noise.

    v5.7.11: operator-controllable suppression via
    ``compliance_audit.zero_row_warning_enabled`` system_setting
    (default True). Flip False on instances that legitimately don't
    see compliance-enforcement-eligible traffic (canary moved away,
    instance dedicated to non-substituting workload, etc.) so the
    daily warning stops without disabling the audit chain itself.
    """
    from app.models.db import ComplianceAuditChain, ActivityLog, SystemSetting
    from sqlalchemy import select, desc

    # v5.7.11 — per-instance opt-out
    try:
        rs0 = await db.execute(
            select(SystemSetting).where(
                SystemSetting.key == "compliance_audit.zero_row_warning_enabled"
            )
        )
        row0 = rs0.scalar_one_or_none()
        if row0 is not None and (row0.value or "").strip().lower() in (
            "false", "0", "no", "off",
        ):
            return
    except Exception:
        # Read failure → keep firing the warning (fail-open for
        # observability, same posture as logging_controls).
        pass

    rs = await db.execute(
        select(ComplianceAuditChain)
        .order_by(desc(ComplianceAuditChain.day))
        .limit(_ZERO_ROW_WARN_THRESHOLD)
    )
    rows = rs.scalars().all()
    if len(rows) < _ZERO_ROW_WARN_THRESHOLD:
        return
    if not all(r.row_count == 0 for r in rows):
        return

    # Suppress duplicate warnings: skip if we've already emitted one for
    # this same starting day in the last 24h.
    streak_oldest_day = rows[-1].day
    look_back = datetime.utcnow() - timedelta(hours=24)
    existing = await db.execute(
        select(ActivityLog)
        .where(ActivityLog.event_type == "audit_chain_zero_row_streak")
        .where(ActivityLog.created_at >= look_back)
        .where(ActivityLog.message.like(f"%streak_start={streak_oldest_day}%"))
        .limit(1)
    )
    if existing.scalar_one_or_none():
        return

    db.add(ActivityLog(
        created_at=datetime.utcnow(),
        severity="warning",
        event_type="audit_chain_zero_row_streak",
        message=(
            f"compliance_audit_chain has signed {_ZERO_ROW_WARN_THRESHOLD} "
            f"consecutive zero-row days (streak_start={streak_oldest_day}, "
            f"streak_end={rows[0].day}). The chain is cryptographically valid "
            f"but no compliance_events have fired in this window — usually a "
            f"sign that the dominant API key has no policy applied (BUG-071) "
            f"or the subsystem is in dry-run mode."
        ),
    ))
    await db.commit()
    logger.warning(
        "compliance_audit.zero_row_streak threshold=%d span=%s..%s",
        _ZERO_ROW_WARN_THRESHOLD, streak_oldest_day, rows[0].day,
    )


def get_last_sweep() -> Dict[str, Any]:
    """Read-only snapshot of the last sweep. Same surface as
    ``prune.get_last_sweep()`` so the admin debug endpoint can read both
    with the same shape."""
    return dict(_LAST_SWEEP_RESULT)


async def _run_one_sweep() -> None:
    """One pass: compute the prior-day integrity hash + purge expired
    events. Both steps are independent — a failure in one doesn't skip
    the other.
    """
    from app.models.database import AsyncSessionLocal
    from app.config import settings

    retention_days = getattr(
        settings, "compliance_audit_retention_days", _DEFAULT_RETENTION_DAYS,
    )
    if not isinstance(retention_days, int) or retention_days < 1:
        retention_days = _DEFAULT_RETENTION_DAYS

    # Step 1 — integrity hash for the closed prior UTC day
    prior_day = (datetime.utcnow() - timedelta(days=1)).date()
    chain_hash: Optional[str] = None
    try:
        async with AsyncSessionLocal() as db:
            chain_hash = await compute_daily_integrity_hash(db, prior_day)
        logger.info(
            "compliance_audit.hash_computed day=%s hash=%s",
            prior_day.isoformat(),
            (chain_hash or "")[:16],
        )
    except Exception as exc:
        logger.warning(
            "compliance_audit.hash_failed day=%s err=%s",
            prior_day.isoformat(),
            exc,
        )

    # Step 1.5 — v5.4.0 (BUG-073): emit warning when N consecutive
    # zero-row days are signed. A fully-signed chain of zero-row days
    # is correct cryptographically but reads as "audit healthy" on
    # inspection when in fact zero events have fired. Common operator
    # mistake: dominant API key has no compliance policy (BUG-071), so
    # the subsystem never enforces and the chain dutifully signs
    # daily zero-row windows.
    try:
        async with AsyncSessionLocal() as db:
            await _emit_zero_row_warning_if_threshold(db, prior_day)
    except Exception as exc:
        logger.warning("compliance_audit.zero_row_check_failed err=%r", exc)

    # Step 2 — retention purge
    purged = 0
    try:
        async with AsyncSessionLocal() as db:
            purged = await purge_expired_events(db, retention_days)
        if purged:
            logger.info(
                "compliance_audit.purged count=%d retention_days=%d",
                purged,
                retention_days,
            )
    except Exception as exc:
        logger.warning("compliance_audit.purge_failed err=%r", exc)

    _LAST_SWEEP_RESULT.update({
        "last_sweep_ts": datetime.utcnow().isoformat(),
        "last_hash_day": prior_day.isoformat(),
        "last_hash": chain_hash,
        "last_purge_count": purged,
        "retention_days": retention_days,
    })


async def _sweep_loop() -> None:
    """Daily loop. Boot-delayed so init_db migrations + first-traffic
    settling finish first."""
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval
    hb = WorkerHeartbeat(name="compliance_audit")
    register_expected_interval("compliance_audit", _SWEEP_INTERVAL_SEC)
    await asyncio.sleep(_INITIAL_DELAY_SEC)
    while True:
        try:
            await _run_one_sweep()
            await hb.tick(
                status="ok",
                note=f"purged={_LAST_SWEEP_RESULT.get('last_purge_count', 0)} "
                     f"day={_LAST_SWEEP_RESULT.get('last_hash_day', '?')}",
            )
        except Exception as exc:
            # Defence-in-depth — _run_one_sweep already catches at the
            # step level; this catches anything unexpected at the loop
            # level so the task never dies silently.
            logger.warning("compliance_audit.sweep_failed err=%r", exc)
            await hb.tick(status="error", note=str(exc)[:200])
        await asyncio.sleep(_SWEEP_INTERVAL_SEC)


_TASK: Optional[asyncio.Task] = None


def start() -> None:
    """Spawn the daily worker. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_sweep_loop(), name="compliance-audit-worker")


def stop() -> None:
    """Cancel the worker. Used by tests; production never stops it."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        _TASK.cancel()
    _TASK = None
