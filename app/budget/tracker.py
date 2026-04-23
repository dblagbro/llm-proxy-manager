"""Budget enforcement + bucket maintenance.

Two call sites:
- `check_budget_pre_request(db, key)` — called from verify_api_key; resets
  stale buckets, raises HTTPException on hard-cap breach, returns
  BudgetStatus for header emission.
- `record_cost(db, key_id, cost_usd)` — called from record_outcome after
  a successful request; increments the per-hour/per-day counters.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ApiKey

logger = logging.getLogger(__name__)


@dataclass
class BudgetStatus:
    """Snapshot after pre-request reset; used to emit X-Budget-* headers."""
    day_cost: float
    day_hard_cap: Optional[float]
    day_soft_cap: Optional[float]
    hour_cost: float
    hour_cap: Optional[float]
    soft_warning: bool  # True if day cost >= soft cap

    @property
    def day_remaining(self) -> Optional[float]:
        if self.day_hard_cap is None:
            return None
        return max(0.0, self.day_hard_cap - self.day_cost)

    @property
    def hour_remaining(self) -> Optional[float]:
        if self.hour_cap is None:
            return None
        return max(0.0, self.hour_cap - self.hour_cost)


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _floor_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


async def check_budget_pre_request(db: AsyncSession, key: ApiKey) -> BudgetStatus:
    """Reset stale buckets; enforce hard caps. Returns snapshot for header emission.

    Raises HTTPException(402) on daily hard cap breach.
    Raises HTTPException(429) on hourly burst cap breach.
    """
    now = datetime.now(timezone.utc)
    current_hour = _floor_hour(now)
    current_day = _floor_day(now)

    # Normalize stored timestamps (strip tz for SQLite compatibility)
    hour_ts = key.hour_bucket_ts
    day_ts = key.day_bucket_ts
    if hour_ts is not None and hour_ts.tzinfo is None:
        hour_ts = hour_ts.replace(tzinfo=timezone.utc)
    if day_ts is not None and day_ts.tzinfo is None:
        day_ts = day_ts.replace(tzinfo=timezone.utc)

    hour_cost = float(key.hour_cost_usd or 0.0)
    day_cost = float(key.day_cost_usd or 0.0)

    # Reset stale buckets
    reset_kwargs: dict = {}
    if hour_ts is None or hour_ts != current_hour:
        hour_cost = 0.0
        reset_kwargs["hour_bucket_ts"] = current_hour.replace(tzinfo=None)
        reset_kwargs["hour_cost_usd"] = 0.0
    if day_ts is None or day_ts != current_day:
        day_cost = 0.0
        reset_kwargs["day_bucket_ts"] = current_day.replace(tzinfo=None)
        reset_kwargs["day_cost_usd"] = 0.0
    if reset_kwargs:
        await db.execute(update(ApiKey).where(ApiKey.id == key.id).values(**reset_kwargs))
        # leave commit to the outer verify_api_key caller

    # Hourly burst cap — 429 (retriable)
    if key.hourly_cap_usd is not None and hour_cost >= key.hourly_cap_usd:
        raise HTTPException(
            429,
            f"Hourly budget exceeded: ${key.hourly_cap_usd:.2f}",
            headers={"Retry-After": "3600"},
        )

    # Daily hard cap — 402 (tenant must top up)
    if key.daily_hard_cap_usd is not None and day_cost >= key.daily_hard_cap_usd:
        raise HTTPException(
            402,
            f"Daily budget exceeded: ${key.daily_hard_cap_usd:.2f}",
        )

    soft_warning = (
        key.daily_soft_cap_usd is not None and day_cost >= key.daily_soft_cap_usd
    )

    return BudgetStatus(
        day_cost=day_cost,
        day_hard_cap=key.daily_hard_cap_usd,
        day_soft_cap=key.daily_soft_cap_usd,
        hour_cost=hour_cost,
        hour_cap=key.hourly_cap_usd,
        soft_warning=soft_warning,
    )


async def record_cost(db: AsyncSession, key_id: str, cost_usd: float) -> None:
    """Increment per-hour and per-day buckets after a successful request.

    Called from record_outcome. Bucket reset is handled by check_budget_pre_request,
    so here we just add — the stored bucket_ts is already current.
    """
    if cost_usd <= 0:
        return
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(
            hour_cost_usd=ApiKey.hour_cost_usd + cost_usd,
            day_cost_usd=ApiKey.day_cost_usd + cost_usd,
        )
    )


def warnings_for(status: BudgetStatus) -> dict[str, str]:
    """Build response headers reflecting current budget state."""
    headers: dict[str, str] = {}
    if status.day_remaining is not None:
        headers["X-Budget-Daily-Remaining"] = f"{status.day_remaining:.4f}"
    if status.hour_remaining is not None:
        headers["X-Budget-Hourly-Remaining"] = f"{status.hour_remaining:.4f}"
    if status.soft_warning and status.day_soft_cap is not None:
        headers["X-Budget-Warning"] = (
            f"daily soft cap ${status.day_soft_cap:.2f} reached"
        )
    return headers
