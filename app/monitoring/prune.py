"""Periodic activity-log + provider-metrics + run-events prune worker (v3.0.7).

The activity_log table grows unbounded — each /v1/messages, /v1/chat/completions,
keep-alive probe, and Run worker event lands a row (often with embedded
request/response bodies up to 50KB each). Without a prune we'll eventually
slow indexed scans and grow the SQLite file beyond what comfortably fits
on a small node.

Strategy (matches the hub team's bot_llm_activity retention plan):
  - Daily sweep, ~24h after startup
  - Default 30-day retention; admin-tunable via ``activity_log_retention_days``
  - Delete in 5000-row batches to avoid locking the DB for long stretches
    (WAL mode helps but a single 30k+ row DELETE can still pin readers)
  - Also prunes provider_metrics (existing ``prune_old_metrics`` helper —
    just hook it into the same loop) and run_events older than retention

Errors are swallowed locally — the prune worker must never block the
serving path or crash the app on a transient DB hiccup.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from sqlalchemy import delete, func, select

from app.config import settings
from app.models.database import AsyncSessionLocal
from app.models.db import ActivityLog, ApiKey, Provider, RunEvent

logger = logging.getLogger(__name__)


_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_PROBE_RETENTION_DAYS = 7   # v3.0.98 — probes age out faster than real traffic
_BATCH_SIZE = 5000
_SWEEP_INTERVAL_SEC = 24 * 60 * 60      # daily
_INITIAL_DELAY_SEC = 60 * 60            # 1h after startup — lets boot settle

# v3.0.13: provider tombstones (deleted_at non-null) hang around so cluster
# sync propagates the soft-delete to peers. Cluster sync runs every 60s, so
# any tombstone older than a few minutes has already converged across the
# fleet — 7 days is a comfortable safety margin before we hard-delete.
_DEFAULT_TOMBSTONE_RETENTION_DAYS = 7


def _retention_days() -> int:
    """Admin-tunable retention; default 30 days, minimum 1 day."""
    try:
        v = int(getattr(settings, "activity_log_retention_days",
                        _DEFAULT_RETENTION_DAYS))
        return max(1, v)
    except Exception:
        return _DEFAULT_RETENTION_DAYS


def _probe_retention_days() -> int:
    """v3.0.98: probe events (api_key_id='probe-keepalive') age out faster
    than real-caller events. With paperless paused, ~80% of fleet traffic
    is keepalive probes — keeping all 30 days of them is wasteful when
    nothing's diagnostic past a week. Minimum is whatever the regular
    retention is (probes never live LONGER than real events).
    """
    try:
        v = int(getattr(settings, "activity_log_probe_retention_days",
                        _DEFAULT_PROBE_RETENTION_DAYS))
        return max(1, min(v, _retention_days()))
    except Exception:
        return _DEFAULT_PROBE_RETENTION_DAYS


def _tombstone_retention_days() -> int:
    """Admin-tunable tombstone retention; default 7 days, minimum 1 day.

    Lower bound prevents an operator from accidentally hard-deleting
    tombstones before they've had a chance to propagate to peers.
    """
    try:
        v = int(getattr(settings, "provider_tombstone_retention_days",
                        _DEFAULT_TOMBSTONE_RETENTION_DAYS))
        return max(1, v)
    except Exception:
        return _DEFAULT_TOMBSTONE_RETENTION_DAYS


async def _prune_provider_tombstones(keep_days: int) -> int:
    """Hard-delete provider rows whose tombstone (``deleted_at``) is older
    than ``keep_days``. Cascades to model_capabilities + model_aliases via
    the existing ON DELETE rules. Returns rows removed.
    """
    deleted = 0
    while True:
        async with AsyncSessionLocal() as db:
            cutoff_expr = func.datetime("now", f"-{keep_days} days")
            id_res = await db.execute(
                select(Provider.id)
                .where(Provider.deleted_at.is_not(None))
                .where(Provider.deleted_at < cutoff_expr)
                .limit(_BATCH_SIZE)
            )
            ids = [r[0] for r in id_res.all()]
            if not ids:
                return deleted
            await db.execute(
                delete(Provider).where(Provider.id.in_(ids))
            )
            await db.commit()
            deleted += len(ids)
        if len(ids) >= _BATCH_SIZE:
            await asyncio.sleep(0.5)
        else:
            return deleted


async def _prune_apikey_tombstones(keep_days: int) -> int:
    """v3.0.20: hard-delete ApiKey rows whose tombstone is older than
    ``keep_days``. Same pattern as the provider tombstone GC — keeps the
    table from growing unbounded with deleted-but-still-replicated rows.
    """
    deleted = 0
    while True:
        async with AsyncSessionLocal() as db:
            cutoff_expr = func.datetime("now", f"-{keep_days} days")
            id_res = await db.execute(
                select(ApiKey.id)
                .where(ApiKey.deleted_at.is_not(None))
                .where(ApiKey.deleted_at < cutoff_expr)
                .limit(_BATCH_SIZE)
            )
            ids = [r[0] for r in id_res.all()]
            if not ids:
                return deleted
            await db.execute(
                delete(ApiKey).where(ApiKey.id.in_(ids))
            )
            await db.commit()
            deleted += len(ids)
        if len(ids) >= _BATCH_SIZE:
            await asyncio.sleep(0.5)
        else:
            return deleted


async def _prune_table(table_class, ts_column, keep_days: int) -> int:
    """Delete rows from ``table_class`` where ``ts_column < cutoff``.

    Done in batches of ``_BATCH_SIZE`` to keep individual transactions
    short (writers can interleave between batches even on a busy node).
    Returns the total rows deleted.
    """
    deleted = 0
    while True:
        async with AsyncSessionLocal() as db:
            cutoff_expr = func.datetime("now", f"-{keep_days} days")
            # SQLite doesn't support DELETE ... LIMIT directly without
            # special compile flags, so do "select N ids → delete by id".
            id_res = await db.execute(
                select(table_class.id).where(ts_column < cutoff_expr).limit(_BATCH_SIZE)
            )
            ids = [r[0] for r in id_res.all()]
            if not ids:
                return deleted
            await db.execute(
                delete(table_class).where(table_class.id.in_(ids))
            )
            await db.commit()
            deleted += len(ids)
        # Brief pause between batches to let writers in
        if len(ids) >= _BATCH_SIZE:
            await asyncio.sleep(0.5)
        else:
            return deleted


async def _prune_probe_events(keep_days: int) -> int:
    """v3.0.98: hard-delete probe-keepalive activity_log rows older than
    ``keep_days`` (typically 7, vs the regular 30-day retention). Probes
    are high-volume low-info events — diagnostic value drops sharply
    after a week (the keepalive trend is captured in metrics buckets).
    """
    deleted = 0
    while True:
        async with AsyncSessionLocal() as db:
            cutoff_expr = func.datetime("now", f"-{keep_days} days")
            id_res = await db.execute(
                select(ActivityLog.id)
                .where(ActivityLog.api_key_id == "probe-keepalive")
                .where(ActivityLog.created_at < cutoff_expr)
                .limit(_BATCH_SIZE)
            )
            ids = [r[0] for r in id_res.all()]
            if not ids:
                return deleted
            await db.execute(delete(ActivityLog).where(ActivityLog.id.in_(ids)))
            await db.commit()
            deleted += len(ids)
        if len(ids) >= _BATCH_SIZE:
            await asyncio.sleep(0.5)
        else:
            return deleted


async def _sweep_once() -> dict:
    """One full prune pass across activity_log + provider_metrics +
    run_events. Returns counts so the log line is interpretable."""
    keep_days = _retention_days()
    probe_keep_days = _probe_retention_days()
    tombstone_days = _tombstone_retention_days()
    out = {"keep_days": keep_days, "probe_keep_days": probe_keep_days,
           "activity_log": 0, "activity_log_probes": 0,
           "provider_metrics": 0, "run_events": 0,
           "provider_tombstones": 0,
           "apikey_tombstones": 0,
           "tombstone_keep_days": tombstone_days}

    # v3.0.98: probes first (shorter retention, high volume).
    try:
        out["activity_log_probes"] = await _prune_probe_events(probe_keep_days)
    except Exception as e:
        logger.warning("prune.activity_log_probes_failed err=%s", e)

    try:
        out["activity_log"] = await _prune_table(
            ActivityLog, ActivityLog.created_at, keep_days,
        )
    except Exception as e:
        logger.warning("prune.activity_log_failed err=%s", e)

    try:
        # Reuse the existing helper for provider_metrics (kept ts comparisons
        # consistent with that path)
        from app.monitoring.metrics import prune_old_metrics
        async with AsyncSessionLocal() as db:
            out["provider_metrics"] = await prune_old_metrics(db, keep_days=keep_days)
    except Exception as e:
        logger.warning("prune.provider_metrics_failed err=%s", e)

    try:
        # run_events uses a float ts (unix seconds) not a DATETIME column —
        # so we can't reuse _prune_table directly without a separate path.
        cutoff_ts = time.time() - (keep_days * 86400)
        deleted = 0
        while True:
            async with AsyncSessionLocal() as db:
                id_res = await db.execute(
                    select(RunEvent.id).where(RunEvent.ts < cutoff_ts).limit(_BATCH_SIZE)
                )
                ids = [r[0] for r in id_res.all()]
                if not ids:
                    break
                await db.execute(delete(RunEvent).where(RunEvent.id.in_(ids)))
                await db.commit()
                deleted += len(ids)
            if len(ids) < _BATCH_SIZE:
                break
            await asyncio.sleep(0.5)
        out["run_events"] = deleted
    except Exception as e:
        logger.warning("prune.run_events_failed err=%s", e)

    try:
        out["provider_tombstones"] = await _prune_provider_tombstones(tombstone_days)
    except Exception as e:
        logger.warning("prune.provider_tombstones_failed err=%s", e)

    try:
        out["apikey_tombstones"] = await _prune_apikey_tombstones(tombstone_days)
    except Exception as e:
        logger.warning("prune.apikey_tombstones_failed err=%s", e)

    return out


async def _prune_loop() -> None:
    """Periodic loop. Sleeps an hour after boot, then sweeps daily."""
    await asyncio.sleep(_INITIAL_DELAY_SEC)
    while True:
        try:
            counts = await _sweep_once()
            _LAST_SWEEP_RESULT.update(counts)
            _LAST_SWEEP_RESULT["last_sweep_ts"] = time.time()
            logger.info(
                "prune.swept activity_log=%d activity_log_probes=%d "
                "provider_metrics=%d run_events=%d provider_tombstones=%d "
                "keep_days=%d probe_keep_days=%d tombstone_keep_days=%d",
                counts["activity_log"], counts.get("activity_log_probes", 0),
                counts["provider_metrics"], counts["run_events"],
                counts["provider_tombstones"], counts["keep_days"],
                counts.get("probe_keep_days", _DEFAULT_PROBE_RETENTION_DAYS),
                counts["tombstone_keep_days"],
            )
        except Exception as e:
            logger.warning("prune.sweep_failed err=%s", e)
        await asyncio.sleep(_SWEEP_INTERVAL_SEC)


# v3.0.98: surface last sweep result via the admin API so operators can
# verify the prune is firing without docker-exec'ing.
_LAST_SWEEP_RESULT: dict = {
    "last_sweep_ts": None,
    "keep_days": _DEFAULT_RETENTION_DAYS,
    "probe_keep_days": _DEFAULT_PROBE_RETENTION_DAYS,
    "tombstone_keep_days": _DEFAULT_TOMBSTONE_RETENTION_DAYS,
}


def get_last_sweep() -> dict:
    """Read-only snapshot of the last sweep counts + retention config.
    Returned as-is from the admin endpoint."""
    return dict(_LAST_SWEEP_RESULT)


_TASK: Optional[asyncio.Task] = None


def start() -> None:
    """Spawn the periodic prune loop. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_prune_loop(), name="activity-prune-loop")
