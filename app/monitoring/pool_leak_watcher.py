"""v5.21.7 — Auto-dump the DB pool trace when utilization crosses a threshold.
v5.21.13 — ...and SELF-HEAL by recycling the pool when saturation is sustained.

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

## v5.21.13 — self-heal (why this exists)

The 2026-07-23 login outage exposed a fatal gap: pre-v5.21.13 this
watcher only *dumped diagnostics*, it never *acted*. When a residual
leak filled the pool (size 50 + overflow 100 = 150) it logged a trace
and did nothing — every DB-backed request, including ``/api/auth/login``,
then 500'd with ``QueuePool ... connection timed out`` until an operator
manually restarted the container. Worse, the safety net defeated
itself: ``worker_heartbeat.tick()`` needs a DB connection to write, so
under full saturation the watcher's OWN heartbeat failed too.

Two design facts make self-heal both possible and safe:

1. **Detection needs no DB.** ``engine.pool.checkedout()`` / ``.size()``
   are in-memory counters — readable even when every connection is
   leaked. So the watcher can always *see* saturation.
2. **Remediation needs no DB.** ``await engine.dispose()`` replaces the
   pool object wholesale: checked-in connections close immediately,
   leaked (never-returned) connections are orphaned with the old pool
   and closed on GC, and the NEW pool starts at full 150 capacity. It
   is the in-process equivalent of a container restart for the DB
   layer — no external connection, no process bounce, no downtime.

So on *sustained* high saturation (``_HEAL_SUSTAINED_POLLS`` consecutive
polls ≥ ``_HEAL_THRESHOLD`` — sustained, so a legitimate load spike that
drains on its own never triggers it), the watcher dumps the forensic
trace (root-cause evidence) and then recycles the pool. A cooldown
prevents thrashing. Result: a residual leak degrades to a periodic
self-recycle logged loudly, never a user-visible outage.
"""
from __future__ import annotations

import asyncio
import logging
import os
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

# ── v5.21.13 self-heal knobs ─────────────────────────────────────────
# Recycle the pool when utilization stays at/above _HEAL_THRESHOLD for
# _HEAL_SUSTAINED_POLLS consecutive samples. "Sustained" is the whole
# point: a real leak only grows, so it holds high across many polls,
# whereas a legitimate burst of concurrent long streams drains on its
# own and resets the counter well before we'd recycle.
_HEAL_THRESHOLD: float = 0.90
_HEAL_SUSTAINED_POLLS: int = 4          # 4 × 30s = ~2 min of sustained saturation
_HEAL_COOLDOWN_SEC: float = 300.0       # don't recycle more than once per 5 min

# Master switch — default ON. Set POOL_SELF_HEAL_ENABLED=0 to fall back
# to dump-only (pre-v5.21.13) behaviour.
_SELF_HEAL_ENABLED: bool = os.getenv("POOL_SELF_HEAL_ENABLED", "1").lower() not in (
    "0", "false", "no", "off",
)

# Self-heal state.
_consecutive_high: int = 0
_last_heal_monotonic: Optional[float] = None

# ── v5.22.0 — aiosqlite THREAD-count monitoring ──────────────────────
# The real leak metric is OS-thread count, NOT pool-slot utilization:
# aiosqlite runs one thread per connection, and threads orphaned on a
# failed connection teardown are invisible to pool.checkedout() (they
# are detached from the pool). engine.dispose() does NOT reclaim them —
# only a process restart does. So we watch the absolute thread count and
# alert LOUDLY when it climbs, which is the detection control that was
# missing when a 7-day node silently reached 232 threads. Alert-only by
# design: orphaned threads can't be reaped in-process, so auto-recycle
# would be theatre. A sustained high count is a "schedule a restart"
# signal, and — with the v5.22.0 non-churning pool — should now never
# fire outside a genuine new regression.
_THREAD_WARN = int(os.getenv("THREAD_LEAK_WARN", "120"))
_THREAD_CRIT = int(os.getenv("THREAD_LEAK_CRIT", "200"))
_thread_armed = {"warn": True, "crit": True}


def _thread_count() -> int:
    try:
        import threading
        return threading.active_count()
    except Exception:
        return -1


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


async def _self_heal_recycle(util: float) -> bool:
    """Recycle the connection pool in-process to reclaim leaked slots.

    Dumps the forensic trace FIRST (so the leaking code path is captured
    right before we throw the evidence away), then ``engine.dispose()``
    replaces the pool. Returns True on success.

    Neither step needs a working DB connection — that's what lets this
    run when the pool is fully starved. Exceptions are swallowed and
    logged: a failed heal must never kill the watcher loop.
    """
    try:
        from app.models.database import engine

        # Snapshot the pool state for the log record (in-memory counters).
        try:
            pool = engine.pool
            before = (
                f"size={pool.size()} checked_out={pool.checkedout()} "
                f"overflow={pool.overflow()}"
            )
        except Exception:
            before = "unavailable"

        # Capture the leaking stacks before disposing (no-op if
        # db_pool_trace is off, but harmless).
        _dump_current_trace(f"self_heal_recycle_util={util:.2f}")

        logger.error(
            "pool_leak_watcher.SELF_HEAL recycling DB pool — sustained "
            "saturation util=%.2f (%s). engine.dispose() reclaims leaked "
            "slots without a container restart.",
            util, before,
        )
        await engine.dispose()
        logger.error("pool_leak_watcher.SELF_HEAL complete — pool recreated fresh")
        return True
    except Exception as exc:
        logger.error("pool_leak_watcher.SELF_HEAL_FAILED err=%r", exc)
        return False


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

                # ── v5.21.13 self-heal ───────────────────────────────
                # Track sustained saturation and recycle the pool when a
                # leak holds it high across several polls. This runs
                # BEFORE heartbeat.tick() (which needs a DB connection and
                # would itself fail under saturation) so remediation never
                # depends on the very resource that's exhausted.
                global _consecutive_high, _last_heal_monotonic
                if _SELF_HEAL_ENABLED and util >= _HEAL_THRESHOLD:
                    _consecutive_high += 1
                    now = time.monotonic()
                    cooled = (
                        _last_heal_monotonic is None
                        or (now - _last_heal_monotonic) >= _HEAL_COOLDOWN_SEC
                    )
                    if _consecutive_high >= _HEAL_SUSTAINED_POLLS and cooled:
                        await _self_heal_recycle(util)
                        _last_heal_monotonic = time.monotonic()
                        _consecutive_high = 0
                else:
                    # Draining (or below threshold) — reset the streak so
                    # a later spike starts counting from zero.
                    _consecutive_high = 0

            # ── v5.22.0 thread-leak detection (DB-free) ──────────────
            # Watch the absolute OS-thread count — the true aiosqlite
            # leak signal. Arm/re-arm like the utilization thresholds so
            # the log isn't spammed while a leak accumulates.
            n_threads = _thread_count()
            if n_threads >= 0:
                if n_threads < _THREAD_WARN:
                    _thread_armed["warn"] = True
                    _thread_armed["crit"] = True
                if n_threads >= _THREAD_CRIT and _thread_armed["crit"]:
                    _thread_armed["crit"] = False
                    logger.error(
                        "pool_leak_watcher.THREAD_LEAK_CRITICAL threads=%d "
                        "(>=%d). aiosqlite connection-thread leak — these are "
                        "orphaned and NOT reclaimable in-process; schedule a "
                        "container restart. Check pool churn / recent config.",
                        n_threads, _THREAD_CRIT,
                    )
                elif n_threads >= _THREAD_WARN and _thread_armed["warn"]:
                    _thread_armed["warn"] = False
                    logger.warning(
                        "pool_leak_watcher.thread_count_high threads=%d (>=%d) "
                        "— watching for aiosqlite connection-thread leak.",
                        n_threads, _THREAD_WARN,
                    )

            # Heartbeat is best-effort and DB-backed: under saturation
            # the write itself fails. Guard it separately so a failed
            # tick never masks the detection/heal work above (which is
            # DB-free and must always run).
            try:
                await heartbeat.tick(
                    status="ok",
                    note=(f"util={util:.2f} threads={n_threads}"
                          if util is not None else f"util=? threads={n_threads}"),
                )
            except Exception as hb_exc:
                logger.debug("pool_leak_watcher.heartbeat_write_failed err=%r", hb_exc)
        except Exception as exc:
            logger.warning("pool_leak_watcher.tick_failed err=%r", exc)
        await asyncio.sleep(_POLL_INTERVAL_SEC)
