"""v4.4.41 — periodic worker that captures Cursor dashboard usage
snapshots for every enabled cursor-oauth provider.

Mirrors ``anthropic_billing_worker.py`` so the cluster-side semantics
(warmup, jitter, freshness guard, per-node duplicate suppression) work
identically across the two vendors. Operator-tunable via the
``cursor_billing_scrape_interval_sec`` setting (default 14400 s = 4 h,
0 = disabled).

Why a separate worker (not just one shared scrape loop): the freshness
guard, the auth-failure path, and the operator-tunable interval are
all already per-vendor in the Anthropic implementation; sharing would
require generalizing all three at once and would muddle a clear
operator-facing "this vendor is misbehaving" signal in the logs.
A 200-LOC dedicated worker is cheaper than the abstraction.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 14400  # 4 hours — matches Anthropic Console cadence
WARMUP_DELAY_SEC = 60
_STARTUP_JITTER_MAX_SEC = 60.0
_TASK: Optional[asyncio.Task] = None


def _interval_sec() -> int:
    try:
        from app.config import settings
        return int(getattr(settings, "cursor_billing_scrape_interval_sec", DEFAULT_INTERVAL_SEC))
    except Exception:
        return DEFAULT_INTERVAL_SEC


def _freshness_floor_sec(interval_sec: int) -> int:
    try:
        from app.config import settings
        override = int(getattr(settings, "cursor_billing_min_scrape_gap_sec", 0))
        if override > 0:
            return override
    except Exception:
        pass
    return max(60, interval_sec // 2)


async def _latest_snapshot_age_sec(db, provider_id: str) -> Optional[float]:
    """Same shape as anthropic_billing_worker._latest_snapshot_age_sec —
    queries the cluster-replicated ExternalUsageSnapshot table for the
    freshest row by this provider, returns age in seconds (or None if
    no snapshot exists yet)."""
    from app.models.db import ExternalUsageSnapshot
    from sqlalchemy import select, func
    from datetime import datetime as _dt

    row = (await db.execute(
        select(func.max(ExternalUsageSnapshot.captured_at))
        .where(ExternalUsageSnapshot.provider_id == provider_id)
    )).scalar()
    if row is None:
        return None
    if isinstance(row, str):
        try:
            cap = _dt.fromisoformat(row.replace("Z", "+00:00"))
        except Exception:
            return None
    else:
        cap = row
    now_naive = _dt.utcnow()
    try:
        cap = cap.replace(tzinfo=None)
    except Exception:
        pass
    delta = (now_naive - cap).total_seconds()
    return max(0.0, delta)


async def _scrape_all_once() -> int:
    """Sweep every enabled cursor-oauth provider whose latest snapshot
    is older than the freshness floor. Returns the count of providers
    actually scraped."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider
    from app.providers.cursor_billing import scrape_provider_into_snapshot
    from sqlalchemy import select

    interval = _interval_sec()
    fresh_floor = _freshness_floor_sec(interval)
    count = 0
    skipped = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Provider)
            .where(Provider.provider_type == "cursor-oauth")
            .where(Provider.deleted_at.is_(None))
            .where(Provider.api_key.is_not(None))
        )
        providers = result.scalars().all()
        for p in providers:
            age = await _latest_snapshot_age_sec(db, p.id)
            if age is not None and age < fresh_floor:
                skipped += 1
                logger.debug(
                    "cursor_billing.skip_fresh",
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
                    "cursor_billing.provider_scrape_crashed",
                    extra={"provider_id": p.id, "error": str(e)},
                )
    if skipped:
        logger.info(
            "cursor_billing.swept scraped=%d skipped_fresh=%d fresh_floor_sec=%d",
            count, skipped, fresh_floor,
        )
    return count


async def _scrape_loop() -> None:
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval
    hb = WorkerHeartbeat(name="cursor_billing")
    jitter = random.uniform(0.0, _STARTUP_JITTER_MAX_SEC)
    await asyncio.sleep(WARMUP_DELAY_SEC + jitter)
    while True:
        interval = _interval_sec()
        register_expected_interval("cursor_billing", interval or 14400)
        if interval <= 0:
            await hb.tick(status="disabled", note=f"interval_sec={interval}")
            await asyncio.sleep(300)
            continue
        try:
            n = await _scrape_all_once()
            if n:
                logger.info("cursor_billing.swept providers=%d", n)
            await hb.tick(status="ok", note=f"scraped={n}")
        except Exception as e:
            logger.warning("cursor_billing.sweep_failed err=%s", e)
            await hb.tick(status="error", note=str(e)[:200])
        await asyncio.sleep(interval)


def start() -> None:
    """Spawn the periodic billing-scrape loop. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_scrape_loop(), name="cursor-billing-scrape-loop")
    logger.info("cursor_billing_worker.started interval_sec=%d", _interval_sec())
