"""v3.7.27 (#245) — periodic worker that captures ChatGPT Plus / Codex
Cloud usage snapshots for every codex-oauth provider that has billing
credentials configured.

Cadence: every 4 hours. Operator-tunable via the
``codex_billing_scrape_interval_sec`` setting (default 14400 s, 0 = disabled).

Mirrors ``app/monitoring/anthropic_billing_worker.py`` exactly,
including the v3.7.24 freshness-guard fix (#258) so peer nodes don't
double-scrape on the same cycle and container restarts don't trigger
extra scrapes inside the freshness window.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 14400  # 4 hours — same as Anthropic billing
WARMUP_DELAY_SEC = 60
_STARTUP_JITTER_MAX_SEC = 60.0
_TASK: Optional[asyncio.Task] = None


def _interval_sec() -> int:
    """Read the live interval setting. 0 disables."""
    try:
        from app.config import settings
        return int(getattr(settings, "codex_billing_scrape_interval_sec", DEFAULT_INTERVAL_SEC))
    except Exception:
        return DEFAULT_INTERVAL_SEC


def _freshness_floor_sec(interval_sec: int) -> int:
    """Minimum gap between scrapes for the same provider (default
    interval/2, minimum 60s). Operator-tunable via
    ``codex_billing_min_scrape_gap_sec`` setting."""
    try:
        from app.config import settings
        override = int(getattr(settings, "codex_billing_min_scrape_gap_sec", 0))
        if override > 0:
            return override
    except Exception:
        pass
    return max(60, interval_sec // 2)


async def _latest_snapshot_age_sec(db, provider_id: str) -> Optional[float]:
    """Age in seconds of the freshest ``chatgpt_codex_v1``-sourced
    snapshot for this provider across the whole cluster. Same pattern
    as ``anthropic_billing_worker._latest_snapshot_age_sec`` but filtered
    by source so the Anthropic snapshots don't accidentally suppress
    Codex scrapes for a provider that happens to have both sets of
    credentials configured.
    """
    from app.models.db import ExternalUsageSnapshot
    from sqlalchemy import select, func
    row = (await db.execute(
        select(func.max(ExternalUsageSnapshot.captured_at))
        .where(ExternalUsageSnapshot.provider_id == provider_id)
        .where(ExternalUsageSnapshot.source == "chatgpt_codex_v1")
    )).scalar()
    if row is None:
        return None
    if isinstance(row, str):
        from datetime import datetime as _dt
        try:
            cap = _dt.fromisoformat(row.replace("Z", "+00:00"))
        except Exception:
            return None
    else:
        cap = row
    from datetime import datetime as _dt
    now_naive = _dt.utcnow()
    try:
        cap = cap.replace(tzinfo=None)
    except Exception:
        pass
    delta = (now_naive - cap).total_seconds()
    return max(0.0, delta)


async def _scrape_all_once() -> int:
    """One sweep: scrape every codex-oauth provider that has codex
    billing credentials configured AND whose latest snapshot is older
    than the freshness floor."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider
    from app.providers.codex_billing import scrape_provider_into_snapshot
    from sqlalchemy import select

    interval = _interval_sec()
    fresh_floor = _freshness_floor_sec(interval)
    count = 0
    skipped = 0
    async with AsyncSessionLocal() as db:
        # v3.8.1 (#245 Phase 2): use the OAuth access_token (Provider.api_key)
        # instead of operator-pasted cookies. The chatgpt.com /backend-api/wham/usage
        # endpoint accepts the same bearer the inference path uses.
        result = await db.execute(
            select(Provider)
            .where(Provider.provider_type == "ChatGPT-oauth-plan")
            .where(Provider.deleted_at.is_(None))
            .where(Provider.api_key.is_not(None))
        )
        providers = result.scalars().all()
        for p in providers:
            age = await _latest_snapshot_age_sec(db, p.id)
            if age is not None and age < fresh_floor:
                skipped += 1
                logger.debug(
                    "codex_billing.skip_fresh",
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
                    "codex_billing.provider_scrape_crashed",
                    extra={"provider_id": p.id, "error": str(e)},
                )
    if skipped:
        logger.info(
            "codex_billing.swept scraped=%d skipped_fresh=%d fresh_floor_sec=%d",
            count, skipped, fresh_floor,
        )
    return count


async def _scrape_loop() -> None:
    """Periodic loop. Random startup jitter + freshness guard mirror
    the v3.7.24 pattern that fixed the Anthropic billing dedup issue."""
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval
    hb = WorkerHeartbeat(name="codex_billing")
    jitter = random.uniform(0.0, _STARTUP_JITTER_MAX_SEC)
    await asyncio.sleep(WARMUP_DELAY_SEC + jitter)
    while True:
        interval = _interval_sec()
        register_expected_interval("codex_billing", interval or 14400)
        if interval <= 0:
            await hb.tick(status="disabled", note=f"interval_sec={interval}")
            await asyncio.sleep(300)
            continue
        try:
            n = await _scrape_all_once()
            if n:
                logger.info("codex_billing.swept providers=%d", n)
            await hb.tick(status="ok", note=f"scraped={n}")
        except Exception as e:
            logger.warning("codex_billing.sweep_failed err=%s", e)
            await hb.tick(status="error", note=str(e)[:200])
        await asyncio.sleep(interval)


def start() -> None:
    """Spawn the periodic codex billing-scrape loop. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_scrape_loop(), name="codex-billing-scrape-loop")
    logger.info("codex_billing_worker.started interval_sec=%d", _interval_sec())
