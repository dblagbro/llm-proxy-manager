"""Per-Run rate limiter (R6).

Spec lock-in (hub-team agreed): a Run can have ``max_turns=30`` spread
over an hour, but a stuck tool-loop emitting tool_use 30 times in 30s
would burn budget fast. Enforce ``runs_max_model_calls_per_minute``
(default 5) per-run; on excess, queue with exponential backoff and emit
a ``rate_limited`` event with ``retry_after_ms / attempt / current_rpm /
limit_rpm`` so observability dashboards can distinguish "transient burst"
from "stuck loop".

Implementation: per-run timestamp deque holding the last N model-call
start times. ``acquire()`` checks if a new call would exceed the limit;
if yes, sleeps for the time-until-oldest-falls-out-of-window plus a
small jitter, returning the wait+attempt for event emission.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)


WINDOW_SEC = 60.0
DEFAULT_LIMIT_RPM = 5
MAX_BACKOFF_SEC = 30.0


class _RunBucket:
    """Tracks model_call_start timestamps for one run; trims to the
    rolling 60s window on each check."""
    __slots__ = ("_starts",)

    def __init__(self) -> None:
        self._starts: deque[float] = deque()

    def _trim(self, now: float) -> None:
        cutoff = now - WINDOW_SEC
        while self._starts and self._starts[0] < cutoff:
            self._starts.popleft()

    def current_rpm(self, now: Optional[float] = None) -> int:
        now = now if now is not None else time.monotonic()
        self._trim(now)
        return len(self._starts)

    def time_until_slot(self, now: float, limit: int) -> float:
        """How long until at least one slot frees in the window."""
        if len(self._starts) < limit:
            return 0.0
        # Oldest in-window timestamp; slot frees once it ages out
        return max(0.0, self._starts[0] + WINDOW_SEC - now)

    def record(self, now: Optional[float] = None) -> None:
        self._starts.append(now if now is not None else time.monotonic())


_buckets: dict[str, _RunBucket] = {}


def _get_bucket(run_id: str) -> _RunBucket:
    b = _buckets.get(run_id)
    if b is None:
        b = _RunBucket()
        _buckets[run_id] = b
    return b


def _resolve_limit() -> int:
    """Pulled from settings each call so admin-edits land without
    restart. Falls back to DEFAULT_LIMIT_RPM when unset/invalid."""
    try:
        from app.config import settings
        v = getattr(settings, "runs_max_model_calls_per_minute", None)
        if v is None:
            return DEFAULT_LIMIT_RPM
        v = int(v)
        if v < 1:
            return DEFAULT_LIMIT_RPM
        return v
    except Exception:
        return DEFAULT_LIMIT_RPM


async def acquire(
    run_id: str,
    *,
    emit_callback=None,
) -> dict:
    """Wait for a free slot; record the new call's timestamp. Returns
    a status dict with attempt count + final wait_ms.

    ``emit_callback`` is an optional async function invoked once if we
    end up waiting; the worker passes a closure that posts a
    ``rate_limited`` event to the broker. Signature:
        async fn(payload: dict) -> None
    """
    bucket = _get_bucket(run_id)
    limit = _resolve_limit()
    attempt = 0
    total_wait_ms = 0.0

    while True:
        now = time.monotonic()
        rpm = bucket.current_rpm(now)
        if rpm < limit:
            bucket.record(now)
            return {
                "attempt": attempt,
                "wait_ms": int(total_wait_ms),
                "rpm_after": rpm + 1,
                "limit_rpm": limit,
            }
        # Over the limit — compute backoff
        attempt += 1
        natural_wait = bucket.time_until_slot(now, limit)
        # Exponential backoff (1s, 2s, 4s, 8s, 16s, capped) + jitter
        backoff = min(MAX_BACKOFF_SEC, 2 ** (attempt - 1))
        jitter = random.uniform(0, 0.5)
        wait = max(natural_wait, backoff) + jitter
        total_wait_ms += wait * 1000.0

        if emit_callback is not None and attempt == 1:
            try:
                await emit_callback({
                    "retry_after_ms": int(wait * 1000),
                    "attempt": attempt,
                    "current_rpm": rpm,
                    "limit_rpm": limit,
                })
            except Exception as e:
                logger.info("runs.rate_limit.emit_skip run=%s err=%s", run_id, e)

        await asyncio.sleep(wait)


def reset(run_id: str) -> None:
    """Drop a run's bucket (called when run terminates so memory bounds)."""
    _buckets.pop(run_id, None)
