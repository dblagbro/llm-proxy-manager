"""v5.7.15 — burst-trigger force-open CB on streaming.empty_success_failover spikes.

Pre-5.7.15 the AI provider supervisor only ran every 30 min. Between sweeps,
a degrading upstream could throw bursts of empty-success streams that v5.7.13
failover wrote to activity_log (via v5.7.14) but nothing acted on. The c1conv
Gemini incident on 2026-06-17 sat in this gap: ~10 empty-successes in 14 min,
no CB open, no supervisor action, just 502s back to coordinator-hub bots.

This worker closes that gap. Cheap DB sweep every 60s:

  SELECT provider_id, COUNT(*) FROM activity_log
  WHERE event_type = 'streaming.empty_success_failover'
    AND created_at > now - empty_success_burst_window_sec
  GROUP BY provider_id
  HAVING COUNT(*) >= empty_success_burst_threshold

For each provider over threshold, force_open its CB (idempotent — a no-op if
already open) and write an audit row so the action is visible in the
dashboard's recent-events panel. No LLM call; no DB write besides the audit.

Independent of ai_provider_supervisor — they share no state; the LLM
classifier still runs every 30 min for slower deprioritize/disable
decisions. This worker only handles the "happening RIGHT NOW" case.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

WARMUP_DELAY_SEC = 60
_STARTUP_JITTER_MAX_SEC = 30.0
_TASK: Optional[asyncio.Task] = None


def _enabled() -> bool:
    try:
        from app.config import settings
        return bool(getattr(settings, "empty_success_burst_trigger_enabled", True))
    except Exception:
        return False


def _interval_sec() -> int:
    try:
        from app.config import settings
        return int(getattr(settings, "empty_success_burst_interval_sec", 60))
    except Exception:
        return 60


def _window_sec() -> int:
    try:
        from app.config import settings
        return int(getattr(settings, "empty_success_burst_window_sec", 300))
    except Exception:
        return 300


def _threshold() -> int:
    try:
        from app.config import settings
        return int(getattr(settings, "empty_success_burst_threshold", 3))
    except Exception:
        return 3


async def _scan_once() -> dict:
    """One scan. Returns counts dict for the heartbeat note + tests."""
    from app.models.database import AsyncSessionLocal
    from app.routing.circuit_breaker import force_open, get_all_states, CBState
    from app.monitoring.activity import log_event

    window = _window_sec()
    threshold = _threshold()
    out = {"detected": 0, "opened": 0, "already_open": 0}

    async with AsyncSessionLocal() as db:
        # Count empty-success failovers per provider in the window.
        # SQLite's strftime keeps the query backend-portable; the index on
        # (event_type) + filter on created_at keeps the scan cheap even
        # at fleet scale (typical activity_log < 1M rows on a single node).
        result = await db.execute(
            text(
                "SELECT provider_id, COUNT(*) AS n FROM activity_log "
                "WHERE event_type = :etype "
                "AND created_at > datetime('now', :window) "
                "AND provider_id IS NOT NULL "
                "GROUP BY provider_id "
                "HAVING COUNT(*) >= :threshold"
            ),
            {
                "etype": "streaming.empty_success_failover",
                "window": f"-{window} seconds",
                "threshold": threshold,
            },
        )
        rows = result.fetchall()
        out["detected"] = len(rows)

        if not rows:
            return out

        states = get_all_states()
        for provider_id, n in rows:
            current = states.get(provider_id, {}).get("state", "closed")
            if current == CBState.OPEN.value:
                out["already_open"] += 1
                continue
            try:
                await force_open(provider_id)
                out["opened"] += 1
                logger.warning(
                    "empty_success_burst_trigger.opened provider=%s "
                    "count=%d window_sec=%d threshold=%d",
                    provider_id, n, window, threshold,
                )
                await log_event(
                    db,
                    event_type="streaming.burst_force_open",
                    severity="warning",
                    message=(
                        f"burst-trigger force-opened CB: {n} empty-success "
                        f"failovers in last {window}s (threshold={threshold})"
                    ),
                    provider_id=provider_id,
                    metadata={
                        "burst_count": int(n),
                        "window_sec": window,
                        "threshold": threshold,
                    },
                )
            except Exception as e:
                logger.warning(
                    "empty_success_burst_trigger.force_open_failed provider=%s err=%r",
                    provider_id, e,
                )
    return out


async def _scan_loop() -> None:
    """Periodic loop. No-op when ``empty_success_burst_trigger_enabled=False``."""
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval
    hb = WorkerHeartbeat(name="empty_success_burst_trigger")
    jitter = random.uniform(0.0, _STARTUP_JITTER_MAX_SEC)
    await asyncio.sleep(WARMUP_DELAY_SEC + jitter)
    while True:
        register_expected_interval("empty_success_burst_trigger", _interval_sec())
        if not _enabled():
            await hb.tick(status="disabled", note="empty_success_burst_trigger_enabled=false")
            await asyncio.sleep(300)
            continue
        try:
            counts = await _scan_once()
            note = (
                f"detected={counts['detected']} "
                f"opened={counts['opened']} "
                f"already_open={counts['already_open']}"
            )
            if counts["opened"]:
                logger.info("empty_success_burst_trigger.swept %s", note)
            await hb.tick(status="ok", note=note)
        except Exception as e:
            logger.warning("empty_success_burst_trigger.sweep_failed err=%s", e)
            await hb.tick(status="error", note=str(e)[:200])
        await asyncio.sleep(_interval_sec())


def start() -> None:
    """Spawn the burst-trigger loop. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_scan_loop(), name="empty-success-burst-trigger-loop")
    logger.info(
        "empty_success_burst_trigger.started — default on; disable via "
        "EMPTY_SUCCESS_BURST_TRIGGER_ENABLED=false",
    )
