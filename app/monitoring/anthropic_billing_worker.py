"""v3.7.0 — periodic worker that captures Anthropic Console usage
snapshots for every claude-oauth provider that has billing
credentials configured.

Cadence: every 4 hours. Operator-tunable via the
``anthropic_billing_scrape_interval_sec`` setting (default 14400 s
= 4 h, 0 = disabled).

Cluster note: this worker runs on every node by default. Each node
gates its own scrape with a per-provider freshness check
(``_latest_snapshot_age_sec``) so a fresh row from any cluster peer
short-circuits duplicate scrapes. Combined with the random startup
jitter, this keeps the visible snapshot count at ~1 row per provider
per cadence cycle even with N nodes.

v3.7.24 (#258) — added the freshness guard + startup jitter. Prior
to this, every container restart triggered a fresh scrape (WARMUP_DELAY)
AND both cluster nodes scraped independently on each cycle, producing
2-4+ visible rows per provider in deploy-heavy windows. Operator saw
"snapshots every few minutes" during back-to-back deploys 2026-05-12.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 14400  # 4 hours
WARMUP_DELAY_SEC = 60         # let cluster sync + providers settle
# v3.7.24: cap per-node startup jitter at 60s so cluster nodes don't all
# fire their first cycle in lockstep after a coordinated deploy. Combined
# with the freshness guard, this means the first node to wake writes the
# canonical snapshot; the rest see it via cluster sync (60s convergence)
# and defer until next cycle.
_STARTUP_JITTER_MAX_SEC = 60.0
_TASK: Optional[asyncio.Task] = None


def _interval_sec() -> int:
    """Read the live interval setting. Default 4h. Setting it to 0
    disables the worker (useful for short-term operator pauses)."""
    try:
        from app.config import settings
        return int(getattr(settings, "anthropic_billing_scrape_interval_sec", DEFAULT_INTERVAL_SEC))
    except Exception:
        return DEFAULT_INTERVAL_SEC


def _freshness_floor_sec(interval_sec: int) -> int:
    """v3.7.24 (#258) — how recent a snapshot must be to short-circuit a
    new scrape. Default = interval/2 (so 2h for a 4h cadence). Skipping
    when a fresher row exists kills the deploy-bounce duplicate (a
    container restart inside the freshness window does not produce a
    redundant scrape) and the cluster-duplicate (after one node wins a
    cycle, the other sees the fresh row via 60s cluster sync and defers).
    Operator-tunable via ``anthropic_billing_min_scrape_gap_sec``.
    """
    try:
        from app.config import settings
        override = int(getattr(settings, "anthropic_billing_min_scrape_gap_sec", 0))
        if override > 0:
            return override
    except Exception:
        pass
    return max(60, interval_sec // 2)


async def _latest_snapshot_age_sec(db, provider_id: str) -> Optional[float]:
    """v3.7.24 (#258) — return the age in seconds of the freshest
    ``external_usage_snapshot`` row for this provider (across the whole
    cluster — cluster sync replicates rows, so this works even if the
    fresh row was written by a peer). Returns ``None`` when no snapshot
    exists yet. Uses a MAX subquery rather than the legacy
    ``ORDER BY captured_at DESC LIMIT 1`` pattern that returns a
    non-deterministic row when the originator and a replica tie on
    timestamp (docs/qa-notes.md "Querying cluster-replicated tables").
    """
    from app.models.db import ExternalUsageSnapshot
    from sqlalchemy import select, func
    row = (await db.execute(
        select(func.max(ExternalUsageSnapshot.captured_at))
        .where(ExternalUsageSnapshot.provider_id == provider_id)
    )).scalar()
    if row is None:
        return None
    # ``captured_at`` is a DATETIME column; SQLA returns either a
    # datetime or a string depending on the dialect. Normalize.
    if isinstance(row, str):
        # SQLite returns "YYYY-MM-DD HH:MM:SS[.fff]"
        from datetime import datetime as _dt
        try:
            cap = _dt.fromisoformat(row.replace("Z", "+00:00"))
        except Exception:
            return None
    else:
        cap = row
    from datetime import datetime as _dt, timezone as _tz
    now_naive = _dt.utcnow()
    # Strip any tz on cap so we can subtract naive-from-naive.
    try:
        cap = cap.replace(tzinfo=None)
    except Exception:
        pass
    delta = (now_naive - cap).total_seconds()
    # Sanity floor — a future timestamp (clock skew between peers) reads
    # as "very fresh"; clamp at 0 so the caller treats it as fresh, not
    # stale.
    return max(0.0, delta)


async def _scrape_all_once() -> int:
    """One sweep: scrape every claude-oauth provider that has
    billing credentials configured AND whose latest snapshot is older
    than the freshness floor. Returns the count of providers actually
    scraped (excludes providers without cookies AND providers skipped
    by the freshness guard)."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider
    from app.providers.anthropic_billing import scrape_provider_into_snapshot
    from sqlalchemy import select

    interval = _interval_sec()
    fresh_floor = _freshness_floor_sec(interval)
    count = 0
    skipped = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Provider)
            .where(Provider.provider_type == "claude-oauth")
            .where(Provider.deleted_at.is_(None))
            .where(Provider.anthropic_org_uuid.is_not(None))
            .where(Provider.anthropic_session_cookies.is_not(None))
        )
        providers = result.scalars().all()
        for p in providers:
            age = await _latest_snapshot_age_sec(db, p.id)
            if age is not None and age < fresh_floor:
                skipped += 1
                logger.debug(
                    "anthropic_billing.skip_fresh",
                    extra={
                        "provider_id": p.id,
                        "snapshot_age_sec": int(age),
                        "fresh_floor_sec": fresh_floor,
                    },
                )
                continue
            try:
                await scrape_provider_into_snapshot(db, p)
                count += 1
            except Exception as e:
                logger.warning(
                    "anthropic_billing.provider_scrape_crashed",
                    extra={"provider_id": p.id, "error": str(e)},
                )
    if skipped:
        logger.info(
            "anthropic_billing.swept scraped=%d skipped_fresh=%d fresh_floor_sec=%d",
            count, skipped, fresh_floor,
        )
    return count


async def _scrape_loop() -> None:
    """Periodic loop. Mirrors the keepalive probe pattern in
    ``app/monitoring/keepalive.py``."""
    # v3.7.24 (#258): warmup + per-node random jitter. Cluster nodes
    # boot at slightly different times, but coordinated deploys can
    # converge them. Adding random[0, 60]s on top of the warmup gives
    # the first-to-fire node ~60s lead over the rest within a deploy
    # window, which combined with the freshness guard prevents all
    # peers from scraping the same cycle.
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval
    hb = WorkerHeartbeat(name="anthropic_billing")
    jitter = random.uniform(0.0, _STARTUP_JITTER_MAX_SEC)
    await asyncio.sleep(WARMUP_DELAY_SEC + jitter)
    while True:
        interval = _interval_sec()
        register_expected_interval("anthropic_billing", interval or 14400)
        if interval <= 0:
            await hb.tick(status="disabled", note=f"interval_sec={interval}")
            await asyncio.sleep(300)  # 5 min before re-checking the disable setting
            continue
        try:
            n = await _scrape_all_once()
            if n:
                logger.info("anthropic_billing.swept providers=%d", n)
            await hb.tick(status="ok", note=f"scraped={n}")
        except Exception as e:
            logger.warning("anthropic_billing.sweep_failed err=%s", e)
            await hb.tick(status="error", note=str(e)[:200])
        await asyncio.sleep(interval)


def start() -> None:
    """Spawn the periodic billing-scrape loop. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_scrape_loop(), name="anthropic-billing-scrape-loop")
    logger.info("anthropic_billing_worker.started interval_sec=%d", _interval_sec())
