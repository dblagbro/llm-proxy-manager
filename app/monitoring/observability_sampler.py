"""v3.9.10 — background sampler that pushes pool depth + scrape
freshness into Prometheus gauges every 30s.

Why a background sampler instead of measure-on-request: Counter and
Histogram metrics update naturally from request handlers, but Gauges
representing "current state" (pool depth, snapshot age) need a
ticker to advance them between requests. Without it, Prometheus
scrapes return whatever the last request happened to set — stale
during idle periods.

Pool snapshot signals to alert on:
    llm_proxy_db_pool_checked_out > size  (saturated, burning overflow)
    llm_proxy_db_pool_checked_out climbing monotonically for N min  (leak)

Scrape freshness alerts:
    llm_proxy_scrape_freshness_seconds{provider=...} > 14400  (4h, the
    default scrape interval — anything past that is a stalled scrape;
    likely cookies expired)
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_INTERVAL_SEC = 30


async def _sample_pool() -> None:
    try:
        from app.models.database import engine
        from app.observability.prometheus import observe_db_pool_snapshot
        pool = engine.pool
        size = pool.size() if hasattr(pool, "size") else 0
        checked_out = pool.checkedout() if hasattr(pool, "checkedout") else 0
        overflow = pool.overflow() if hasattr(pool, "overflow") else 0
        observe_db_pool_snapshot(size, checked_out, overflow)
    except Exception as e:
        logger.debug(f"observability_sampler.pool err={e!r}")


async def _sample_scrape_freshness() -> None:
    try:
        from sqlalchemy import select, desc
        from app.models.database import AsyncSessionLocal
        from app.models.db import Provider, ExternalUsageSnapshot
        from app.observability.prometheus import observe_scrape_freshness

        async with AsyncSessionLocal() as db:
            provs = (await db.execute(
                select(Provider)
                .where(Provider.deleted_at.is_(None))
                .where(Provider.usage_tracking_enabled == True)  # noqa: E712
            )).scalars().all()
            now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
            for p in provs:
                snap = (await db.execute(
                    select(ExternalUsageSnapshot)
                    .where(ExternalUsageSnapshot.provider_id == p.id)
                    .order_by(desc(ExternalUsageSnapshot.captured_at))
                    .limit(1)
                )).scalar_one_or_none()
                if snap is None or snap.captured_at is None:
                    # Skip providers that have never been scraped — emitting
                    # an "infinity" gauge would noise up dashboards. They
                    # show up in /api/providers without a usage_data_source.
                    continue
                age_sec = (now_naive - snap.captured_at).total_seconds()
                observe_scrape_freshness(
                    provider_id=p.id,
                    provider_name=p.name,
                    source=snap.source or "unknown",
                    age_sec=max(0.0, age_sec),
                )
    except Exception as e:
        logger.debug(f"observability_sampler.scrape err={e!r}")


async def _loop() -> None:
    # Boot delay so we don't fight startup migrations / first scrape.
    await asyncio.sleep(15)
    while True:
        await _sample_pool()
        await _sample_scrape_freshness()
        await asyncio.sleep(_INTERVAL_SEC)


_task: asyncio.Task | None = None


def start() -> None:
    """Idempotent start. Called from app/main.py startup hook."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _task = loop.create_task(_loop())
        logger.info("observability_sampler.started interval=%ss", _INTERVAL_SEC)
    except Exception as e:
        logger.warning(f"observability_sampler.start_failed err={e!r}")
