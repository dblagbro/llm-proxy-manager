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
from app.models.db import (
    ActivityLog, ApiKey, ApiKeyAiReview, ExternalUsageSnapshot,
    Provider, ProviderAiReview, RunEvent,
)

logger = logging.getLogger(__name__)


_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_PROBE_RETENTION_DAYS = 7   # v3.0.98 — probes age out faster than real traffic
# v3.7.22 (#254) — severity-tiered retention. info events dominate volume
# (~99% in steady state) and lose diagnostic value within ~30d. warning/error
# events are rare and post-mortem-valuable for much longer; default to 1y
# and 5y respectively so a slow-fuse bug discovered weeks later still has
# evidence.
_DEFAULT_WARNING_RETENTION_DAYS = 365
_DEFAULT_ERROR_RETENTION_DAYS = 1825
_BATCH_SIZE = 5000
_SWEEP_INTERVAL_SEC = 24 * 60 * 60      # daily
_INITIAL_DELAY_SEC = 60 * 60            # 1h after startup — lets boot settle

# v3.0.13: provider tombstones (deleted_at non-null) hang around so cluster
# sync propagates the soft-delete to peers. Cluster sync runs every 60s, so
# any tombstone older than a few minutes has already converged across the
# fleet — 7 days is a comfortable safety margin before we hard-delete.
_DEFAULT_TOMBSTONE_RETENTION_DAYS = 7

# v5.9.8 (#472) — external_usage_snapshot retention. Each provider with the
# Anthropic Console scraper enabled writes one row per 4-hour window
# (~6/day per provider). With 11 providers on the clone cluster that's
# ~66 rows/day; 90 days = ~6k rows, modest but worth pruning so the table
# doesn't grow without bound across years of operation. The rotation
# evaluator only consults the latest snapshot per provider, so older
# rows have no live consumer — they're observational. Default 90d.
_DEFAULT_EXTERNAL_USAGE_RETENTION_DAYS = 90


def _retention_days() -> int:
    """Admin-tunable retention; default 30 days, minimum 1 day.

    v5.1.2 / Batch C3 — prefers the operator override stored in
    system_settings (``compliance.activity_log_retention_days``).
    The retention_settings module caches the override; the sweep
    loop calls refresh_from_db() before each pass so a UI edit
    takes effect within one sweep cycle.
    """
    try:
        from app.monitoring.retention_settings import info_days
        return int(info_days())
    except Exception:
        pass
    try:
        v = int(getattr(settings, "activity_log_retention_days",
                        _DEFAULT_RETENTION_DAYS))
        return max(1, v)
    except Exception:
        return _DEFAULT_RETENTION_DAYS


def _warning_retention_days() -> int:
    """Admin-tunable warning-severity retention; default 365 days."""
    try:
        from app.monitoring.retention_settings import warning_days
        return int(warning_days())
    except Exception:
        pass
    try:
        v = int(getattr(settings, "activity_log_warning_retention_days",
                        _DEFAULT_WARNING_RETENTION_DAYS))
        return max(1, v)
    except Exception:
        return _DEFAULT_WARNING_RETENTION_DAYS


def _error_retention_days() -> int:
    """Admin-tunable error-severity retention; default 1825 days (5y)."""
    try:
        from app.monitoring.retention_settings import error_days
        return int(error_days())
    except Exception:
        pass
    try:
        v = int(getattr(settings, "activity_log_error_retention_days",
                        _DEFAULT_ERROR_RETENTION_DAYS))
        return max(1, v)
    except Exception:
        return _DEFAULT_ERROR_RETENTION_DAYS


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


async def _prune_activity_log_by_severity(severity: str, keep_days: int) -> int:
    """v3.7.22 (#254) — delete activity_log rows of a specific severity
    older than ``keep_days``. Lets info events age out at the regular
    cadence while preserving warning/error events for longer post-mortem
    windows.
    """
    deleted = 0
    while True:
        async with AsyncSessionLocal() as db:
            cutoff_expr = func.datetime("now", f"-{keep_days} days")
            id_res = await db.execute(
                select(ActivityLog.id)
                .where(ActivityLog.severity == severity)
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


def _ai_review_retention_days() -> int:
    """v4.4.13 — admin-tunable AI-review retention; default 30 days,
    minimum 1 day."""
    try:
        v = int(getattr(settings, "ai_review_retention_days", 30))
        return max(1, v)
    except Exception:
        return 30


async def _prune_ai_review(model_cls, keep_days: int) -> int:
    """v4.4.13 — delete rows from an AI-review table older than
    ``keep_days``. Same batched pattern as the other prune helpers
    (capped by ``_BATCH_SIZE``; sleep between batches to avoid
    holding the WAL). Generic over either ``ApiKeyAiReview`` or
    ``ProviderAiReview`` since both have a ``captured_at`` DATETIME
    column and an integer ``id`` PK."""
    deleted = 0
    while True:
        async with AsyncSessionLocal() as db:
            cutoff_expr = func.datetime("now", f"-{keep_days} days")
            id_res = await db.execute(
                select(model_cls.id)
                .where(model_cls.captured_at < cutoff_expr)
                .limit(_BATCH_SIZE)
            )
            ids = [r[0] for r in id_res.all()]
            if not ids:
                return deleted
            await db.execute(delete(model_cls).where(model_cls.id.in_(ids)))
            await db.commit()
            deleted += len(ids)
        if len(ids) >= _BATCH_SIZE:
            await asyncio.sleep(0.5)
        else:
            return deleted


async def _prune_activity_log_orphans() -> int:
    """v4.4.3 BUG-055 — delete activity_log rows whose ``provider_id``
    or ``api_key_id`` no longer points at a real row. Provider /
    api_key tombstones get hard-deleted after
    ``provider_tombstone_retention_days`` (default 7); any activity
    log row that referenced that id then becomes a dangling
    foreign key (SQLite has no enforcement, so they silently
    accumulate). Pre-fix audit on 2026-05-20 found 438 + 7937 such
    rows on www1 — historical cost-attribution / forensic queries
    that JOIN to providers / api_keys silently lose those rows.

    Runs daily as part of ``_sweep_once``, after the severity /
    probe / tombstone prune steps so we're operating on the
    post-prune set. Batched at ``_BATCH_SIZE`` like the other
    prunes."""
    deleted = 0
    # Two separate DELETEs (one per FK column) — easier to read and
    # easier to interpret in the sweep log line. Each loop uses the
    # same NOT IN scalar subquery pattern as the rest of the prune
    # helpers in this module.
    while True:
        async with AsyncSessionLocal() as db:
            id_res = await db.execute(
                select(ActivityLog.id)
                .where(ActivityLog.provider_id.is_not(None))
                .where(~ActivityLog.provider_id.in_(select(Provider.id)))
                .limit(_BATCH_SIZE)
            )
            ids = [r[0] for r in id_res.all()]
            if not ids:
                break
            await db.execute(delete(ActivityLog).where(ActivityLog.id.in_(ids)))
            await db.commit()
            deleted += len(ids)
        if len(ids) < _BATCH_SIZE:
            break
        await asyncio.sleep(0.5)

    while True:
        async with AsyncSessionLocal() as db:
            id_res = await db.execute(
                select(ActivityLog.id)
                .where(ActivityLog.api_key_id.is_not(None))
                .where(~ActivityLog.api_key_id.in_(select(ApiKey.id)))
                .limit(_BATCH_SIZE)
            )
            ids = [r[0] for r in id_res.all()]
            if not ids:
                break
            await db.execute(delete(ActivityLog).where(ActivityLog.id.in_(ids)))
            await db.commit()
            deleted += len(ids)
        if len(ids) < _BATCH_SIZE:
            break
        await asyncio.sleep(0.5)

    return deleted


def _external_usage_retention_days() -> int:
    """v5.9.8 (#472) — external_usage_snapshot retention. Settings
    override → env → default cascade, same shape as the other helpers."""
    try:
        v = int(getattr(settings, "external_usage_snapshot_retention_days",
                        _DEFAULT_EXTERNAL_USAGE_RETENTION_DAYS))
        return max(1, v)
    except (TypeError, ValueError):
        return _DEFAULT_EXTERNAL_USAGE_RETENTION_DAYS


async def _prune_external_usage_snapshots(keep_days: int) -> int:
    """v5.9.8 (#472) — delete external_usage_snapshot rows with
    captured_at < cutoff. Batched same as the other prunes."""
    return await _prune_table(
        ExternalUsageSnapshot, ExternalUsageSnapshot.captured_at, keep_days
    )


async def _sweep_once() -> dict:
    """One full prune pass across activity_log + provider_metrics +
    run_events. Returns counts so the log line is interpretable."""
    # v5.1.2 / Batch C3 — refresh the retention override cache before
    # reading. A UI edit takes effect on the very next sweep without
    # waiting for the soft TTL.
    try:
        from app.monitoring.retention_settings import refresh_from_db
        from app.models.database import AsyncSessionLocal
        async with AsyncSessionLocal() as _db:
            await refresh_from_db(_db)
    except Exception:
        # Best-effort; helpers fall back to env on cache-miss anyway.
        pass

    keep_days = _retention_days()
    probe_keep_days = _probe_retention_days()
    warning_keep_days = _warning_retention_days()
    error_keep_days = _error_retention_days()
    tombstone_days = _tombstone_retention_days()
    external_usage_keep_days = _external_usage_retention_days()
    out = {"keep_days": keep_days, "probe_keep_days": probe_keep_days,
           "warning_keep_days": warning_keep_days,
           "error_keep_days": error_keep_days,
           "activity_log": 0, "activity_log_probes": 0,
           "activity_log_warnings": 0, "activity_log_errors": 0,
           "activity_log_orphans": 0,
           "provider_metrics": 0, "run_events": 0,
           "provider_tombstones": 0,
           "apikey_tombstones": 0,
           "tombstone_keep_days": tombstone_days,
           "provider_ai_reviews": 0, "api_key_ai_reviews": 0,
           "ai_review_keep_days": _ai_review_retention_days(),
           "external_usage_snapshots": 0,
           "external_usage_keep_days": external_usage_keep_days,
           "wal_truncated": {"busy": 0, "log_pages": 0, "ckpt_pages": 0,
                             "size_before": 0, "size_after": 0}}

    # v3.0.98: probes first (shorter retention, high volume).
    try:
        out["activity_log_probes"] = await _prune_probe_events(probe_keep_days)
    except Exception as e:
        logger.warning("prune.activity_log_probes_failed err=%s", e)

    # v3.7.22 (#254): severity-tiered retention. info uses the legacy
    # ``activity_log_retention_days`` setting (default 30d); warning and
    # error severities use their own longer retentions.
    try:
        out["activity_log"] = await _prune_activity_log_by_severity(
            "info", keep_days,
        )
    except Exception as e:
        logger.warning("prune.activity_log_info_failed err=%s", e)

    try:
        out["activity_log_warnings"] = await _prune_activity_log_by_severity(
            "warning", warning_keep_days,
        )
    except Exception as e:
        logger.warning("prune.activity_log_warnings_failed err=%s", e)

    try:
        out["activity_log_errors"] = await _prune_activity_log_by_severity(
            "error", error_keep_days,
        )
    except Exception as e:
        logger.warning("prune.activity_log_errors_failed err=%s", e)

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

    # v4.4.3 BUG-055 — orphan activity_log refs. MUST run after the
    # provider/apikey tombstone prunes above, because the orphan
    # query identifies "activity_log rows whose FK no longer
    # exists" — and the tombstone prunes are what create new
    # orphans on each sweep.
    try:
        out["activity_log_orphans"] = await _prune_activity_log_orphans()
    except Exception as e:
        logger.warning("prune.activity_log_orphans_failed err=%s", e)

    # v4.4.13 — prune the AI supervisor review tables. Unbounded
    # growth surfaced 2026-05-21: 1561 rows on www1 + 1384 on www2 in
    # ~6 days, bloating the cluster-sync payload to 2.78 MB and
    # causing www2 to time out (the sync timeout was raised 15→45s
    # in the same release as belt-and-braces). Default retention is
    # ``ai_review_retention_days`` (30d).
    try:
        out["provider_ai_reviews"] = await _prune_ai_review(
            ProviderAiReview, _ai_review_retention_days()
        )
    except Exception as e:
        logger.warning("prune.provider_ai_reviews_failed err=%s", e)
    try:
        out["api_key_ai_reviews"] = await _prune_ai_review(
            ApiKeyAiReview, _ai_review_retention_days()
        )
    except Exception as e:
        logger.warning("prune.api_key_ai_reviews_failed err=%s", e)

    # v5.9.8 (#472) — external_usage_snapshot. 4-hourly billing-scrape
    # rows per provider; only the latest is consumed by rotation logic.
    # Older rows are observational, so retention can be relatively short
    # (90d default) without losing live functionality.
    try:
        out["external_usage_snapshots"] = await _prune_external_usage_snapshots(
            external_usage_keep_days
        )
    except Exception as e:
        logger.warning("prune.external_usage_snapshots_failed err=%s", e)

    # v4.4.4 BUG-052 — WAL high-water reclaim. SQLite reuses WAL pages
    # in place across PASSIVE checkpoints, so a past burst (e.g. the
    # 2026-05-13 RMAI 1.04B-token amplifier loop that drove 27× normal
    # proxy volume) leaves the WAL file at its high-water indefinitely.
    # MUST run LAST in the sweep because the prune steps above are
    # exactly the heavy writes that re-populate the WAL — checkpoint
    # them out so the TRUNCATE can fully reclaim the file.
    try:
        out["wal_truncated"] = await _wal_checkpoint_truncate()
    except Exception as e:
        logger.warning("prune.wal_checkpoint_failed err=%s", e)

    return out


async def _wal_checkpoint_truncate() -> dict:
    """v4.4.4 BUG-052 — TRUNCATE-mode WAL checkpoint at end of each
    sweep. Reclaims any high-water WAL file space left over from a
    write burst (e.g. the 2026-05-13 RMAI amplifier on www1 left the
    WAL at 1.097 GB until manually truncated 2026-05-20).

    Returns ``{busy, log_pages, ckpt_pages, size_before, size_after}``
    so the daily sweep log line shows what was reclaimed.

    Safety: TRUNCATE mode blocks if any reader is mid-transaction
    (returns busy=1). The sweep runs once daily and is a no-op when
    busy — the next sweep will retry, and a long-running reader is
    its own anomaly worth investigating separately."""
    from sqlalchemy import text
    import os
    sizes = {"size_before": 0, "size_after": 0}
    # Best-effort file-size capture; if the path resolution fails
    # (test DBs, in-memory, etc.) the function still attempts the
    # checkpoint with the size deltas as 0.
    try:
        db_url = str(getattr(settings, "database_url", "") or "")
        # sqlite+aiosqlite:////app/data/llmproxy.db → /app/data/llmproxy.db
        if "sqlite" in db_url:
            path = db_url.split("///")[-1]
            wal_path = path + "-wal"
            if os.path.exists(wal_path):
                sizes["size_before"] = os.path.getsize(wal_path)
    except Exception:
        pass
    busy = log_pages = ckpt_pages = 0
    async with AsyncSessionLocal() as db:
        try:
            r = await db.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            row = r.first()
            if row:
                busy, log_pages, ckpt_pages = row[0], row[1], row[2]
        except Exception as e:
            logger.warning("prune.wal_checkpoint_execute_failed err=%s", e)
    try:
        if sizes["size_before"]:
            wal_path = db_url.split("///")[-1] + "-wal"
            if os.path.exists(wal_path):
                sizes["size_after"] = os.path.getsize(wal_path)
    except Exception:
        pass
    return {
        "busy": busy,
        "log_pages": log_pages,
        "ckpt_pages": ckpt_pages,
        **sizes,
    }


async def _prune_loop() -> None:
    """Periodic loop. Sleeps an hour after boot, then sweeps daily."""
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval
    hb = WorkerHeartbeat(name="prune")
    register_expected_interval("prune", _SWEEP_INTERVAL_SEC)
    await asyncio.sleep(_INITIAL_DELAY_SEC)
    while True:
        try:
            counts = await _sweep_once()
            _LAST_SWEEP_RESULT.update(counts)
            _LAST_SWEEP_RESULT["last_sweep_ts"] = time.time()
            wal = counts.get("wal_truncated", {}) or {}
            wal_reclaimed = max(0, (wal.get("size_before") or 0) - (wal.get("size_after") or 0))
            logger.info(
                "prune.swept activity_log=%d activity_log_probes=%d "
                "activity_log_warnings=%d activity_log_errors=%d "
                "activity_log_orphans=%d "
                "provider_metrics=%d run_events=%d provider_tombstones=%d "
                "provider_ai_reviews=%d api_key_ai_reviews=%d "
                "external_usage_snapshots=%d "
                "wal_reclaimed_bytes=%d wal_busy=%d "
                "keep_days=%d probe_keep_days=%d warning_keep_days=%d "
                "error_keep_days=%d tombstone_keep_days=%d "
                "ai_review_keep_days=%d external_usage_keep_days=%d",
                counts["activity_log"], counts.get("activity_log_probes", 0),
                counts.get("activity_log_warnings", 0),
                counts.get("activity_log_errors", 0),
                counts.get("activity_log_orphans", 0),
                counts["provider_metrics"], counts["run_events"],
                counts["provider_tombstones"],
                counts.get("provider_ai_reviews", 0),
                counts.get("api_key_ai_reviews", 0),
                counts.get("external_usage_snapshots", 0),
                wal_reclaimed, wal.get("busy", 0),
                counts["keep_days"],
                counts.get("probe_keep_days", _DEFAULT_PROBE_RETENTION_DAYS),
                counts.get("warning_keep_days", _DEFAULT_WARNING_RETENTION_DAYS),
                counts.get("error_keep_days", _DEFAULT_ERROR_RETENTION_DAYS),
                counts["tombstone_keep_days"],
                counts.get("ai_review_keep_days", 30),
                counts.get("external_usage_keep_days",
                           _DEFAULT_EXTERNAL_USAGE_RETENTION_DAYS),
            )
            await hb.tick(
                status="ok",
                note=f"activity_log={counts.get('activity_log',0)} provider_metrics={counts.get('provider_metrics',0)} run_events={counts.get('run_events',0)}",
            )
        except Exception as e:
            logger.warning("prune.sweep_failed err=%s", e)
            await hb.tick(status="error", note=str(e)[:200])
        await asyncio.sleep(_SWEEP_INTERVAL_SEC)


# v3.0.98: surface last sweep result via the admin API so operators can
# verify the prune is firing without docker-exec'ing.
_LAST_SWEEP_RESULT: dict = {
    "last_sweep_ts": None,
    "keep_days": _DEFAULT_RETENTION_DAYS,
    "probe_keep_days": _DEFAULT_PROBE_RETENTION_DAYS,
    "warning_keep_days": _DEFAULT_WARNING_RETENTION_DAYS,
    "error_keep_days": _DEFAULT_ERROR_RETENTION_DAYS,
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
