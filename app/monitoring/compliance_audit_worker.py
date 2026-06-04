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
        logger.warning("compliance_audit.purge_failed err=%s", exc)

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
    await asyncio.sleep(_INITIAL_DELAY_SEC)
    while True:
        try:
            await _run_one_sweep()
        except Exception as exc:
            # Defence-in-depth — _run_one_sweep already catches at the
            # step level; this catches anything unexpected at the loop
            # level so the task never dies silently.
            logger.warning("compliance_audit.sweep_failed err=%s", exc)
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
