"""v3.9.13 (#267 follow-up) — TTL sweeper for caller_memory rows.

Hub team asked for per-key TTL: "if you want a per-key TTL config later
(background sweeper that tombstones rows where updated_at < now - N
days for keys that opt in), ping me and I'll add it". This is that.

Operator opt-in is per-api-key via the new ``api_keys.caller_memory_ttl_days``
column. NULL = no TTL (rows persist; current behavior). Integer = sweeper
tombstones any CallerMemory row whose owner api_key has that TTL set
AND whose ``updated_at`` is older than the threshold.

Why a sweeper instead of TTL-on-read: read-time eviction would force
every inject hot-path call to compute "is this row expired?" and either
return None or skip — adds DB queries to the request path. A background
sweep is async, batched, and the inject path stays as a single index
lookup.

Cadence: hourly by default (``caller_memory_ttl_sweep_interval_sec``,
default 3600). Operators with tighter retention can tune down; the
sweep is cheap (one JOIN query + a tombstone UPDATE per match).

Tombstones propagate via the existing LWW cluster sync — peers see the
``deleted_at`` and mirror it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, update

logger = logging.getLogger(__name__)


DEFAULT_INTERVAL_SEC = 3600  # 1h


def _interval_sec() -> int:
    try:
        from app.config import settings
        v = int(getattr(settings, "caller_memory_ttl_sweep_interval_sec", DEFAULT_INTERVAL_SEC))
        return max(60, v)  # don't sweep faster than 1 min
    except Exception:
        return DEFAULT_INTERVAL_SEC


async def _sweep_once() -> dict:
    """One pass: tombstone CallerMemory rows whose owner api_key has
    caller_memory_ttl_days set AND row is older than the TTL.

    Returns {"keys_with_ttl": int, "rows_tombstoned": int}.
    """
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey, CallerMemory

    now_unix = time.time()
    stats = {"keys_with_ttl": 0, "rows_tombstoned": 0}

    async with AsyncSessionLocal() as db:
        # Step 1: find every api_key with TTL set + not deleted
        keys = (await db.execute(
            select(ApiKey.id, ApiKey.caller_memory_ttl_days)
            .where(ApiKey.caller_memory_ttl_days.is_not(None))
            .where(ApiKey.deleted_at.is_(None))
        )).all()
        stats["keys_with_ttl"] = len(keys)
        if not keys:
            return stats

        for api_key_id, ttl_days in keys:
            if ttl_days is None or ttl_days <= 0:
                continue
            cutoff = now_unix - (int(ttl_days) * 86400)
            # Step 2: tombstone matching rows for this key
            sel = (await db.execute(
                select(CallerMemory)
                .where(CallerMemory.api_key_id == api_key_id)
                .where(CallerMemory.deleted_at.is_(None))
                .where(CallerMemory.updated_at < cutoff)
            )).scalars().all()
            for row in sel:
                row.deleted_at = now_unix
                row.updated_at = now_unix  # bump for LWW cluster propagation
                stats["rows_tombstoned"] += 1
            # Invalidate Redis for tombstoned rows
            try:
                from app.memory.store import _get_redis, _key
                r = await _get_redis()
                if r is not None:
                    for row in sel:
                        try:
                            await r.delete(_key(row.api_key_id, row.conversation_id, row.memory_tag))
                        except Exception:
                            pass
            except Exception:
                pass

        await db.commit()

    if stats["rows_tombstoned"]:
        logger.info(
            "caller_memory_ttl_sweeper.swept "
            f"keys_with_ttl={stats['keys_with_ttl']} "
            f"rows_tombstoned={stats['rows_tombstoned']}"
        )
    return stats


async def _loop() -> None:
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval
    hb = WorkerHeartbeat(name="caller_memory_ttl_sweeper")
    # Boot delay so we don't fight startup migrations
    await asyncio.sleep(120)
    while True:
        interval = _interval_sec()
        register_expected_interval("caller_memory_ttl_sweeper", interval or 3600)
        try:
            await _sweep_once()
            await hb.tick(status="ok", note="swept")
        except Exception as e:
            logger.warning(f"caller_memory_ttl_sweeper.loop err={e!r}")
            await hb.tick(status="error", note=str(e)[:200])
        await asyncio.sleep(interval)


_task: asyncio.Task | None = None


def start() -> None:
    """Idempotent start. No-op when caller_memory_enabled=False —
    nothing to sweep."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        from app.config import settings
        if not getattr(settings, "caller_memory_enabled", False):
            logger.info("caller_memory_ttl_sweeper.skipped (caller_memory_enabled=False)")
            return
        loop = asyncio.get_event_loop()
        _task = loop.create_task(_loop())
        logger.info(
            "caller_memory_ttl_sweeper.started interval=%ss",
            _interval_sec(),
        )
    except Exception as e:
        logger.warning(f"caller_memory_ttl_sweeper.start_failed err={e!r}")
