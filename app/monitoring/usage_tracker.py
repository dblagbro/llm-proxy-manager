"""Per-provider usage-window tracking (v3.0.62).

Background task computes rolling token totals for OAuth-style "session + weekly
quota" providers (claude-oauth, codex-oauth, anthropic-oauth, and any future
grok-oauth / azure-oauth). Operator opts in per provider via
``providers.usage_tracking_enabled``.

Two windows tracked per enabled provider:

- **Session**: rolling window of ``usage_session_window_sec`` (claude.ai default 5h).
- **Weekly**: since the most-recent reset boundary defined by
  ``usage_weekly_reset_dow`` (0=Mon, 6=Sun) + ``usage_weekly_reset_hour`` (0-23 local).
  Claude.ai resets Sunday 4pm = dow=6, hour=16.

Cached results live in ``provider_usage_windows`` so reads are O(1). The task
recomputes every ``USAGE_TRACKER_INTERVAL_SEC`` (default 60s).

Phase 1 (this version): measurement + read API only. No rotation logic — that
ships in Phase 3 once operator has visibility into the numbers and tunes the
limits/thresholds via the Phase 2 UI.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import settings
from app.models.database import AsyncSessionLocal
from app.models.db import Provider, ProviderUsageWindow

logger = logging.getLogger(__name__)


_INTERVAL_SEC_DEFAULT = 60


def _interval_sec() -> int:
    try:
        v = int(getattr(settings, "usage_tracker_interval_sec", _INTERVAL_SEC_DEFAULT))
        return max(15, v)  # don't pound the DB faster than 15s
    except Exception:
        return _INTERVAL_SEC_DEFAULT


def _last_weekly_reset_at(now: datetime, dow: int, hour: int) -> datetime:
    """Compute the most-recent weekly-reset wall-clock instant <= now.

    dow: 0=Monday ... 6=Sunday (matches Python's datetime.weekday()).
    hour: 0..23 local hour (we treat ``now`` as local time naive).

    Returns a tz-naive datetime; the caller compares against tz-naive
    ``activity_log.created_at`` (also tz-naive in our DB).
    """
    # Walk back at most 7 days to find the previous occurrence of (dow, hour).
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    days_back = (candidate.weekday() - dow) % 7
    candidate = candidate - timedelta(days=days_back)
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


async def _compute_one(provider: Provider) -> Optional[dict]:
    """Compute one provider's session + weekly totals. Returns the dict to
    upsert, or None if tracking is disabled / not configured enough."""
    if not provider.usage_tracking_enabled:
        return None

    session_window_sec = provider.usage_session_window_sec or 18000  # 5h default
    now = datetime.now()  # local-naive; matches activity_log timestamps
    session_start = now - timedelta(seconds=session_window_sec)

    weekly_start: Optional[datetime] = None
    if provider.usage_weekly_reset_dow is not None and provider.usage_weekly_reset_hour is not None:
        weekly_start = _last_weekly_reset_at(
            now, provider.usage_weekly_reset_dow, provider.usage_weekly_reset_hour,
        )
    weekly_reset_at = weekly_start + timedelta(days=7) if weekly_start else None

    async with AsyncSessionLocal() as db:
        # Session sum — IMPORTANT: aggregate over llm_request rows for this provider.
        # We sum (in_tok + out_tok) from the JSON event_meta. The event_meta path
        # avoids a separate table; SQLite handles json_extract natively.
        session_q = await db.execute(
            select(
                func.coalesce(
                    func.sum(
                        func.cast(
                            func.json_extract(_event_meta_col(), "$.in_tok"), Integer  # type: ignore[arg-type]
                        )
                        + func.cast(
                            func.json_extract(_event_meta_col(), "$.out_tok"), Integer  # type: ignore[arg-type]
                        )
                    ),
                    0,
                ),
            ).select_from(_activity_log_tbl())
            .where(_activity_log_tbl().c.provider_id == provider.id)
            .where(_activity_log_tbl().c.event_type == "llm_request")
            .where(_activity_log_tbl().c.severity == "info")
            .where(_activity_log_tbl().c.created_at >= session_start)
        )
        session_tokens = int(session_q.scalar_one() or 0)

        weekly_tokens = 0
        if weekly_start is not None:
            weekly_q = await db.execute(
                select(
                    func.coalesce(
                        func.sum(
                            func.cast(
                                func.json_extract(_event_meta_col(), "$.in_tok"), Integer  # type: ignore[arg-type]
                            )
                            + func.cast(
                                func.json_extract(_event_meta_col(), "$.out_tok"), Integer  # type: ignore[arg-type]
                            )
                        ),
                        0,
                    ),
                ).select_from(_activity_log_tbl())
                .where(_activity_log_tbl().c.provider_id == provider.id)
                .where(_activity_log_tbl().c.event_type == "llm_request")
                .where(_activity_log_tbl().c.severity == "info")
                .where(_activity_log_tbl().c.created_at >= weekly_start)
            )
            weekly_tokens = int(weekly_q.scalar_one() or 0)

    session_pct = None
    if provider.usage_session_limit_tokens:
        session_pct = round(100.0 * session_tokens / provider.usage_session_limit_tokens, 2)
    weekly_pct = None
    if provider.usage_weekly_limit_tokens:
        weekly_pct = round(100.0 * weekly_tokens / provider.usage_weekly_limit_tokens, 2)

    return {
        "provider_id": provider.id,
        "session_tokens": session_tokens,
        "session_window_start": session_start,
        "session_pct": session_pct,
        "weekly_tokens": weekly_tokens,
        "weekly_reset_at": weekly_reset_at,
        "weekly_pct": weekly_pct,
    }


# Lazy imports — Integer / activity_log table are referenced via lambdas below
# to avoid a circular import at module load. Keeps this module free of the
# heavyweight model imports until the task actually runs.
def _activity_log_tbl():
    from app.models.db import ActivityLog  # type: ignore
    return ActivityLog.__table__


def _event_meta_col():
    return _activity_log_tbl().c.event_meta


from sqlalchemy import Integer  # noqa: E402  -- needed for func.cast above


async def _sweep_once() -> int:
    """One pass over all tracking-enabled providers. Returns count updated."""
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(Provider).where(
                Provider.enabled == True,  # noqa: E712
                Provider.deleted_at.is_(None),
                Provider.usage_tracking_enabled == True,  # noqa: E712
            )
        )
        providers = list(res.scalars().all())

    count = 0
    for p in providers:
        try:
            row = await _compute_one(p)
            if row is None:
                continue
            async with AsyncSessionLocal() as db2:
                stmt = sqlite_insert(ProviderUsageWindow).values(**row)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["provider_id"],
                    set_={k: v for k, v in row.items() if k != "provider_id"},
                )
                await db2.execute(stmt)
                await db2.commit()
            count += 1
        except Exception as e:
            logger.warning("usage_tracker.compute_failed provider=%s err=%s", p.id, e)
    return count


async def _loop() -> None:
    """Periodic loop. Fires the first sweep ~30s after startup, then on the
    configured interval."""
    await asyncio.sleep(30)
    while True:
        interval = _interval_sec()
        try:
            n = await _sweep_once()
            if n:
                logger.debug("usage_tracker.swept count=%d", n)
        except Exception as e:
            logger.warning("usage_tracker.sweep_failed err=%s", e)
        await asyncio.sleep(interval)


_TASK: Optional[asyncio.Task] = None


def start() -> None:
    """Spawn the periodic compute loop. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop(), name="usage-tracker-loop")
