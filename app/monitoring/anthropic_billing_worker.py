"""v3.7.0 — periodic worker that captures Anthropic Console usage
snapshots for every claude-oauth provider that has billing
credentials configured.

Cadence: every 4 hours. Operator-tunable via the
``anthropic_billing_scrape_interval_sec`` setting (default 14400 s
= 4 h, 0 = disabled).

Cluster note: this worker runs on every node by default. Three nodes
× 6 scrapes/day per provider = 18 hits per provider per day, which
is well within polite use of an internal Anthropic endpoint not
designed for scraping. If Anthropic ever rate-limits us, switch to
primary-node-only by gating on ``settings.cluster_node_id ==
settings.cluster_primary_node_id``.

The first scrape fires ~60s after startup so providers/cluster sync
have settled. Subsequent scrapes use the configured interval.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 14400  # 4 hours
WARMUP_DELAY_SEC = 60         # let cluster sync + providers settle
_TASK: Optional[asyncio.Task] = None


def _interval_sec() -> int:
    """Read the live interval setting. Default 4h. Setting it to 0
    disables the worker (useful for short-term operator pauses)."""
    try:
        from app.config import settings
        return int(getattr(settings, "anthropic_billing_scrape_interval_sec", DEFAULT_INTERVAL_SEC))
    except Exception:
        return DEFAULT_INTERVAL_SEC


async def _scrape_all_once() -> int:
    """One sweep: scrape every claude-oauth provider that has
    billing credentials configured. Returns the count of providers
    actually scraped (excludes providers without cookies)."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider
    from app.providers.anthropic_billing import scrape_provider_into_snapshot
    from sqlalchemy import select

    count = 0
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
            try:
                await scrape_provider_into_snapshot(db, p)
                count += 1
            except Exception as e:
                logger.warning(
                    "anthropic_billing.provider_scrape_crashed",
                    extra={"provider_id": p.id, "error": str(e)},
                )
    return count


async def _scrape_loop() -> None:
    """Periodic loop. Mirrors the keepalive probe pattern in
    ``app/monitoring/keepalive.py``."""
    await asyncio.sleep(WARMUP_DELAY_SEC)
    while True:
        interval = _interval_sec()
        if interval <= 0:
            await asyncio.sleep(300)  # 5 min before re-checking the disable setting
            continue
        try:
            n = await _scrape_all_once()
            if n:
                logger.info("anthropic_billing.swept providers=%d", n)
        except Exception as e:
            logger.warning("anthropic_billing.sweep_failed err=%s", e)
        await asyncio.sleep(interval)


def start() -> None:
    """Spawn the periodic billing-scrape loop. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_scrape_loop(), name="anthropic-billing-scrape-loop")
    logger.info("anthropic_billing_worker.started interval_sec=%d", _interval_sec())
