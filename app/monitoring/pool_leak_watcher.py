"""v5.21.7 — Auto-dump the DB pool trace when utilization crosses a threshold.

Companion to the SIGUSR2 dumper (v5.21.6). SIGUSR2 needs an operator
to manually invoke it after noticing symptoms; this watcher catches
the leak signature BEFORE exhaustion by dumping automatically when
the pool crosses configurable utilization thresholds.

Fires at each threshold (50%, 75%, 90%) at most ONCE per pool-usage
"crossing" — i.e. once utilization drops back below the threshold,
the next re-crossing arms the alert again. That keeps the logs from
being spammed while a leak is accumulating.

The dump content is identical to the SIGUSR2 handler — sorted
oldest-first async session traces with the caller's stack. That's
what identifies the leaking code path.

Wired at app startup as one of the ``worker_heartbeat``-registered
background workers. Runs every 30 seconds (cheap — just a size check).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Utilization thresholds. Order matters: we fire at the LOWEST unfired
# threshold that's been crossed. When utilization drops back below a
# threshold, that threshold re-arms.
_THRESHOLDS: tuple[float, ...] = (0.50, 0.75, 0.90)

# Sample interval — how often the watcher checks the pool. 30s is a
# compromise: fast enough to catch a growing leak within one full pool
# turnover, slow enough to add negligible overhead.
_POLL_INTERVAL_SEC = 30

# For each threshold, whether it's currently "armed" (i.e. ready to
# fire on next crossing). Starts True (armed). Fires → False. Drops
# back below → True.
_armed: dict[float, bool] = {t: True for t in _THRESHOLDS}


def _get_pool_utilization() -> Optional[float]:
    """Return current utilization (0.0-1.0) or None if unavailable.
    Reads the same numbers ``/health`` exposes."""
    try:
        from app.models.database import engine
        pool = engine.pool
        size = pool.size()
        checked_out = pool.checkedout()
        # Utilization is checked_out / total-cap. If overflow is enabled
        # (SQLAlchemy allows up to size+max_overflow), we cap at 1.0.
        # Effective cap: size + max_overflow. Since default is 50+100=150,
        # utilization = checked_out / 150.
        max_overflow = getattr(pool, "_max_overflow", 100)
        total_cap = size + max_overflow
        if total_cap <= 0:
            return None
        return min(1.0, checked_out / total_cap)
    except Exception as exc:
        logger.debug("pool_leak_watcher: util-read failed: %r", exc)
        return None


def _dump_current_trace(reason: str) -> None:
    """Dump the async-session trace + a header saying WHY this dump
    fired. Same shape as the SIGUSR2 handler's output so operators
    can parse either interchangeably."""
    try:
        from app.models.database import (
            get_async_session_trace, get_pool_checkout_trace,
        )
        async_sessions = get_async_session_trace()
        pool_checkouts = get_pool_checkout_trace()

        logger.warning(
            "pool_leak_watcher.auto_dump reason=%s async_sessions=%d sync_checkouts=%d",
            reason, len(async_sessions), len(pool_checkouts),
        )
        top_n = 20
        for i, entry in enumerate(async_sessions[:top_n]):
            stack = entry.get("stack", "")
            app_frames = [
                line.strip() for line in stack.split("\n")
                if "/app/" in line or "app/api" in line or "app/routing" in line
                or "app/monitoring" in line or "app/models" in line
            ]
            leaf_frames = app_frames[-6:] if len(app_frames) > 6 else app_frames
            logger.warning(
                "  #%d age=%.1fs session_id=%s",
                i, entry.get("age_sec", 0.0),
                entry.get("session_id", "?")[:12],
            )
            for line in leaf_frames:
                logger.warning("      %s", line[:160])
        if len(async_sessions) > top_n:
            logger.warning(
                "  … +%d more async sessions (dump capped at %d)",
                len(async_sessions) - top_n, top_n,
            )
    except Exception as exc:
        logger.warning("pool_leak_watcher.dump_failed err=%r", exc)


async def pool_leak_watcher_loop() -> None:
    """Background loop. Runs forever. Registered via WorkerHeartbeat
    so the /health monitoring shows whether it's alive."""
    from app.monitoring.worker_heartbeat import WorkerHeartbeat
    heartbeat = WorkerHeartbeat(
        name="pool_leak_watcher",
        expected_interval_sec=_POLL_INTERVAL_SEC,
    )

    logger.info(
        "pool_leak_watcher.started thresholds=%s poll_interval=%ss",
        _THRESHOLDS, _POLL_INTERVAL_SEC,
    )
    while True:
        try:
            util = _get_pool_utilization()
            if util is not None:
                # Re-arm thresholds we've dropped below
                for threshold in _THRESHOLDS:
                    if util < threshold and not _armed[threshold]:
                        _armed[threshold] = True
                        logger.info(
                            "pool_leak_watcher.rearmed threshold=%.2f util=%.2f",
                            threshold, util,
                        )
                # Fire at the HIGHEST crossed-and-armed threshold
                # (higher thresholds are more urgent — if we're at 92%,
                # fire the 90% alert, not the 50% one).
                for threshold in reversed(_THRESHOLDS):
                    if util >= threshold and _armed[threshold]:
                        _armed[threshold] = False
                        _dump_current_trace(
                            f"utilization_crossed_{int(threshold*100)}pct"
                        )
                        break
            await heartbeat.tick(
                status="ok",
                note=f"util={util:.2f}" if util is not None else "util=?",
            )
        except Exception as exc:
            try:
                await heartbeat.tick(status="error", note=str(exc)[:120])
            except Exception:
                pass
            logger.warning("pool_leak_watcher.tick_failed err=%r", exc)
        await asyncio.sleep(_POLL_INTERVAL_SEC)
