"""v5.14.2 — cluster-sync 403-rate alert worker (closes #492 escalation trigger).

Reads the v5.14.2 in-process metric from ``cluster_sync_metrics.snapshot()``
every ``cluster_sync_403_monitor_interval_sec`` (default 300s). When the
rolling-1h ``recent_403_pct`` crosses ``cluster_sync_403_alert_threshold_pct``
(default 70.0), emits a single ``severity=warning`` activity_log row of
``event_type=cluster_sync.403_rate_elevated``.

The baseline-aware ceiling (70%) is set above the historical known-bad
baseline of ~50% on tmrwww02 peer so the worker does not fire on the
status-quo noise. When operator fixes the misconfig and the baseline drops
to ~0%, the same threshold catches any future regression. If the baseline
ever climbs above 70% as a "new normal" the operator just bumps
``cluster_sync_403_alert_threshold_pct`` to re-silence.

Re-arm semantics: after firing, the worker enters a cooldown of
``cluster_sync_403_alert_cooldown_sec`` (default 3600s) to avoid duplicate
rows on a sustained event. The cooldown reads from process memory; restart
re-arms immediately.

Min-sample guard: skips evaluation entirely when ``attempts_1h <
cluster_sync_403_alert_min_attempts`` (default 4) so a single 403 in an
idle window doesn't fire (1/1 = 100%).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

logger = logging.getLogger(__name__)

WARMUP_DELAY_SEC = 90
_STARTUP_JITTER_MAX_SEC = 30.0
_TASK: Optional[asyncio.Task] = None
_last_fired_at: float = 0.0


def _enabled() -> bool:
    try:
        from app.config import settings
        return bool(getattr(settings, "cluster_sync_403_monitor_enabled", True))
    except Exception:
        return False


def _interval_sec() -> int:
    try:
        from app.config import settings
        return int(getattr(settings, "cluster_sync_403_monitor_interval_sec", 300))
    except Exception:
        return 300


def _alert_threshold_pct() -> float:
    try:
        from app.config import settings
        return float(getattr(settings, "cluster_sync_403_alert_threshold_pct", 70.0))
    except Exception:
        return 70.0


def _alert_min_attempts() -> int:
    try:
        from app.config import settings
        return int(getattr(settings, "cluster_sync_403_alert_min_attempts", 4))
    except Exception:
        return 4


def _alert_cooldown_sec() -> int:
    try:
        from app.config import settings
        return int(getattr(settings, "cluster_sync_403_alert_cooldown_sec", 3600))
    except Exception:
        return 3600


async def _scan_once() -> dict:
    """One scan. Returns the decision dict (for tests + heartbeat note)."""
    global _last_fired_at

    from app.monitoring.cluster_sync_metrics import snapshot
    from app.monitoring.activity import log_event

    snap = snapshot()
    threshold = _alert_threshold_pct()
    min_attempts = _alert_min_attempts()
    cooldown = _alert_cooldown_sec()

    decision = {
        "fired": False,
        "reason": None,
        "snapshot": snap,
        "threshold_pct": threshold,
    }

    if snap["attempts_1h"] < min_attempts:
        decision["reason"] = f"min_attempts:{snap['attempts_1h']}<{min_attempts}"
        return decision

    if snap["recent_403_pct"] < threshold:
        decision["reason"] = f"under_threshold:{snap['recent_403_pct']}<{threshold}"
        return decision

    now = time.time()
    if now - _last_fired_at < cooldown:
        remaining = int(cooldown - (now - _last_fired_at))
        decision["reason"] = f"cooldown:{remaining}s_remaining"
        return decision

    # Fire.
    _last_fired_at = now
    try:
        await log_event(
            event_type="cluster_sync.403_rate_elevated",
            severity="warning",
            message=(
                f"Cluster-sync 403 rate elevated: "
                f"{snap['recent_403_pct']:.1f}% over last 1h "
                f"({snap['status_403_1h']}/{snap['attempts_1h']} attempts), "
                f"threshold={threshold:.1f}%"
            ),
            event_meta={
                "recent_403_pct": snap["recent_403_pct"],
                "attempts_1h": snap["attempts_1h"],
                "status_403_1h": snap["status_403_1h"],
                "status_200_1h": snap["status_200_1h"],
                "threshold_pct": threshold,
                "cluster_sync_fresh": snap["cluster_sync_fresh"],
            },
        )
        decision["fired"] = True
        decision["reason"] = "alerted"
    except Exception as e:
        decision["fired"] = False
        decision["reason"] = f"log_event_failed:{type(e).__name__}"
        logger.warning(f"cluster_sync_403_monitor log_event failed: {e}")
    return decision


async def _loop() -> None:
    """Worker loop. Runs while enabled; sleeps interval between scans."""
    from app.monitoring.worker_heartbeat import (
        WorkerHeartbeat, register_expected_interval,
    )
    hb = WorkerHeartbeat(name="cluster_sync_403_monitor")
    jitter = random.uniform(0.0, _STARTUP_JITTER_MAX_SEC)
    await asyncio.sleep(WARMUP_DELAY_SEC + jitter)

    while True:
        register_expected_interval("cluster_sync_403_monitor", _interval_sec())
        if not _enabled():
            await hb.tick(
                status="disabled",
                note="cluster_sync_403_monitor_enabled=false",
            )
            await asyncio.sleep(300)
            continue
        try:
            decision = await _scan_once()
            note = (
                f"reason={decision.get('reason')} "
                f"403_pct={decision['snapshot'].get('recent_403_pct')}% "
                f"attempts={decision['snapshot'].get('attempts_1h')}"
            )
            if decision["fired"]:
                logger.info("cluster_sync_403_monitor.alerted %s", note)
            await hb.tick(status="ok", note=note)
        except Exception as e:
            logger.warning("cluster_sync_403_monitor.scan_failed err=%s", e)
            await hb.tick(status="error", note=str(e)[:200])
        await asyncio.sleep(_interval_sec())


def start() -> None:
    """Spawn the monitor loop. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop(), name="cluster-sync-403-monitor-loop")
    logger.info(
        "cluster_sync_403_monitor.started — default on; disable via "
        "CLUSTER_SYNC_403_MONITOR_ENABLED=false",
    )
