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


# v3.10.4 — aggregate error-rate alert. ``_sample_error_rate`` runs
# every _ERROR_RATE_CHECK_EVERY ticks (30s each → ~5 min).
_ERROR_RATE_CHECK_EVERY = 10
_tick = 0


def _should_alert_error_rate(
    err: int, total: int, min_count: int, threshold_pct: float,
) -> bool:
    """Pure decision: alert when the window has at least ``min_count``
    operator-actionable errors AND they are at least ``threshold_pct``
    of all requests. ``min_count`` is the low-traffic noise floor — it
    stops a handful of errors in a near-idle window from paging."""
    if total <= 0 or err < min_count:
        return False
    return (err * 100.0 / total) >= threshold_pct


async def _sample_error_rate() -> None:
    try:
        from app.config import settings
        if not getattr(settings, "error_rate_alert_enabled", True):
            return
        from collections import Counter
        from datetime import timedelta
        from sqlalchemy import select
        from app.models.database import AsyncSessionLocal
        from app.models.db import ActivityLog

        window = int(getattr(settings, "error_rate_alert_window_min", 15))
        threshold = float(getattr(settings, "error_rate_alert_threshold_pct", 10.0))
        min_count = int(getattr(settings, "error_rate_alert_min_count", 10))
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=window)

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(ActivityLog.severity, ActivityLog.message,
                       ActivityLog.event_meta)
                .where(ActivityLog.created_at >= cutoff)
                .where(ActivityLog.event_type == "llm_request")
            )).all()

        total = err = 0
        classes: "Counter[str]" = Counter()
        for sev, msg, meta in rows:
            if msg and "[probe]" in msg:
                continue  # keepalive probes aren't user traffic
            m = meta if isinstance(meta, dict) else {}
            # v3.10.14 BUG-026 — internal-source traffic (the AI
            # supervisor's own classifier calls etc.) is not user
            # traffic; exclude it from the error-rate alert signal.
            if m.get("internal_source"):
                continue
            total += 1
            if sev in ("error", "critical"):
                err += 1
                ec = m.get("error_class")
                classes[ec or "unknown"] += 1

        if not _should_alert_error_rate(err, total, min_count, threshold):
            return
        rate = err * 100.0 / total
        top = ", ".join(f"{c}×{n}" for c, n in classes.most_common(3)) or "n/a"
        from app.monitoring.notifications import alert_high_error_rate
        await alert_high_error_rate(err, total, rate, window, top)
        logger.warning(
            "observability_sampler.high_error_rate err=%d total=%d rate=%.1f%% top=%s",
            err, total, rate, top,
        )
    except Exception as e:
        logger.debug(f"observability_sampler.error_rate err={e!r}")


_infra_fault_baseline: "float | None" = None


async def _sample_infra_errors() -> None:
    """v3.10.15 BUG-032 — surface a rising count of genuine ASGI / DB-pool
    faults. The infra-error tap counts them on ``llm_proxy_infra_errors_total``;
    this turns a sustained climb into a log warning so a pool-exhaustion or
    ASGI-crash incident is noticed even though those errors never reach
    ``activity_log`` (so the v3.10.4 error-rate alert cannot see them).
    Benign client-disconnects (``fault_class="disconnect"``) are ignored."""
    global _infra_fault_baseline
    try:
        from app.observability.prometheus import INFRA_ERRORS_TOTAL
        faults = 0.0
        for metric in INFRA_ERRORS_TOTAL.collect():
            for s in metric.samples:
                if s.name.endswith("_total") and s.labels.get("fault_class") == "fault":
                    faults += s.value
        if _infra_fault_baseline is None:
            _infra_fault_baseline = faults
            return
        delta = faults - _infra_fault_baseline
        _infra_fault_baseline = faults
        if delta >= 5:
            logger.warning(
                "observability_sampler.infra_faults_climbing delta=%.0f "
                "total=%.0f in ~%ds — check ASGI / DB-pool health "
                "(llm_proxy_infra_errors_total)",
                delta, faults, _INTERVAL_SEC,
            )
    except Exception as e:
        logger.debug(f"observability_sampler.infra_errors err={e!r}")


async def _loop() -> None:
    global _tick
    # Boot delay so we don't fight startup migrations / first scrape.
    await asyncio.sleep(15)
    while True:
        await _sample_pool()
        await _sample_scrape_freshness()
        await _sample_infra_errors()
        _tick += 1
        if _tick % _ERROR_RATE_CHECK_EVERY == 0:
            await _sample_error_rate()
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
