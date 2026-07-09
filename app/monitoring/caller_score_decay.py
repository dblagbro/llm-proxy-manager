"""v5.10.0 Ship 2 — background decay of caller_capability_score.

Fires every 6h. Multiplies all score values by ~0.96 (= 0.85^0.25),
giving a ~24h half-life. Prunes rows below the GC threshold so the
table stays bounded.

Uses the standard WorkerHeartbeat pattern (v5.4.0 — BUG-069/074) so
the supervisor sweep sees the worker's last-tick heartbeat and can
alarm if it stalls.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.models.database import AsyncSessionLocal
from app.monitoring.worker_heartbeat import WorkerHeartbeat

logger = logging.getLogger(__name__)


SWEEP_INTERVAL_SEC = 6 * 60 * 60  # 6h
_TASK: Optional[asyncio.Task] = None
_HEARTBEAT = WorkerHeartbeat("caller-score-decay", expected_interval_sec=SWEEP_INTERVAL_SEC)


async def _sweep_once() -> tuple[int, int]:
    """One decay tick. Returns (updated_count, gc_count)."""
    from app.capability_scout.score import decay_all_scores
    async with AsyncSessionLocal() as db:
        return await decay_all_scores(db)


async def _decay_loop() -> None:
    """Repeating loop. First fire is delayed one interval so the very
    first sweep doesn't happen during cold-start, when other workers
    are also competing for DB."""
    # Initial delay — be patient at startup.
    try:
        await asyncio.sleep(SWEEP_INTERVAL_SEC)
    except asyncio.CancelledError:
        return
    while True:
        try:
            updated, gc = await _sweep_once()
            logger.info(
                "caller_score_decay.swept updated=%d gc=%d", updated, gc
            )
            await _HEARTBEAT.beat()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("caller_score_decay tick failed: %s", exc)
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SEC)
        except asyncio.CancelledError:
            return


def start_decay_worker() -> None:
    """Idempotent: spawned once from main.py lifespan."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_decay_loop(), name="caller-score-decay-loop")
    logger.info("caller_score_decay worker started (interval=%ds)", SWEEP_INTERVAL_SEC)


async def stop_decay_worker() -> None:
    """Cancel + await the task for clean shutdown."""
    global _TASK
    if _TASK is None:
        return
    _TASK.cancel()
    try:
        await _TASK
    except (asyncio.CancelledError, Exception):
        pass
    _TASK = None
