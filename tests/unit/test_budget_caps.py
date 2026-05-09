"""Per-key budget cap enforcement tests.

The defensive caps set fleet-wide on 2026-05-09 (paperless / coord-hub
/ devinGPT / tax-ai-analyzer) ride this code path. If
``check_budget_pre_request`` regresses, a runaway caller could burn
through the API quota before anything else catches it (paperless
2026-05-02 burned $151 in 48h before the operator paused the service).

Coverage:
- hourly_cap_usd → 429 with Retry-After
- daily_hard_cap_usd → 402
- daily_soft_cap_usd → soft_warning flag (no exception)
- bucket reset on hour/day rollover
- absent caps → no enforcement (default behavior preserved)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.budget.tracker import (
    BudgetStatus,
    check_budget_pre_request,
    record_cost,
)


# ── Test fixture ──────────────────────────────────────────────────────


@pytest.fixture
async def fresh_key(request):
    """Insert a single ApiKey row and return it. Cleanup at teardown."""
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, ApiKey
    from sqlalchemy import delete

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKey).where(ApiKey.id == "test-budget-key"))
        await db.commit()
        k = ApiKey(
            id="test-budget-key",
            name="test",
            key_prefix="llmp-test",
            key_hash="test-hash",
            enabled=True,
        )
        db.add(k)
        await db.commit()
        await db.refresh(k)

    yield  # tests look up the key themselves

    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ApiKey).where(ApiKey.id == "test-budget-key"))
        await cleanup.commit()


async def _get_key():
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(ApiKey).where(ApiKey.id == "test-budget-key"))).scalar_one()


# ── No-cap baseline ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_caps_no_enforcement(fresh_key):
    """A key with no caps should sail through, returning a BudgetStatus
    that signals 'no cap' (None on day_remaining and hour_remaining)."""
    from app.models.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        key = (await db.execute(select(__import__("app.models.db", fromlist=["ApiKey"]).ApiKey).where(__import__("app.models.db", fromlist=["ApiKey"]).ApiKey.id == "test-budget-key"))).scalar_one()
        status = await check_budget_pre_request(db, key)
        await db.commit()
    assert isinstance(status, BudgetStatus)
    assert status.day_remaining is None
    assert status.hour_remaining is None
    assert status.soft_warning is False


# ── Hard daily cap ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_daily_hard_cap_blocks_at_threshold(fresh_key):
    """Once day_cost ≥ daily_hard_cap, every subsequent request gets
    402. Set up: day cost = $10.00, hard cap = $10.00 → blocked."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey
    async with AsyncSessionLocal() as db:
        key = (await db.execute(select(ApiKey).where(ApiKey.id == "test-budget-key"))).scalar_one()
        # Pre-set day bucket already at cap
        now = datetime.now(timezone.utc)
        key.day_bucket_ts = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        key.day_cost_usd = 10.0
        key.daily_hard_cap_usd = 10.0
        await db.commit()

        with pytest.raises(HTTPException) as ex:
            await check_budget_pre_request(db, key)
        assert ex.value.status_code == 402
        assert "Daily budget exceeded" in ex.value.detail


@pytest.mark.asyncio
async def test_daily_hard_cap_allows_under_threshold(fresh_key):
    """day_cost < daily_hard_cap → no exception."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey
    async with AsyncSessionLocal() as db:
        key = (await db.execute(select(ApiKey).where(ApiKey.id == "test-budget-key"))).scalar_one()
        now = datetime.now(timezone.utc)
        key.day_bucket_ts = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        key.day_cost_usd = 9.99
        key.daily_hard_cap_usd = 10.0
        await db.commit()

        status = await check_budget_pre_request(db, key)
        await db.commit()
    assert status.day_remaining == pytest.approx(0.01, abs=1e-9)


# ── Hourly burst cap ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hourly_cap_blocks_at_threshold(fresh_key):
    """Hourly burst protection: hits 429 with Retry-After=3600 so
    callers retry-after-an-hour rather than tight-loop."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey
    async with AsyncSessionLocal() as db:
        key = (await db.execute(select(ApiKey).where(ApiKey.id == "test-budget-key"))).scalar_one()
        now = datetime.now(timezone.utc)
        key.hour_bucket_ts = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
        key.hour_cost_usd = 5.0
        key.hourly_cap_usd = 5.0
        await db.commit()

        with pytest.raises(HTTPException) as ex:
            await check_budget_pre_request(db, key)
        assert ex.value.status_code == 429
        assert "Hourly budget exceeded" in ex.value.detail
        assert ex.value.headers.get("Retry-After") == "3600"


# ── Soft cap → warning, no block ───────────────────────────────────────


@pytest.mark.asyncio
async def test_daily_soft_cap_sets_warning_flag(fresh_key):
    """Soft cap doesn't block; just sets soft_warning so callers see
    an X-Budget-Warning header. Acts as an early-alert before hard cap."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey
    async with AsyncSessionLocal() as db:
        key = (await db.execute(select(ApiKey).where(ApiKey.id == "test-budget-key"))).scalar_one()
        now = datetime.now(timezone.utc)
        key.day_bucket_ts = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        key.day_cost_usd = 5.0
        key.daily_soft_cap_usd = 5.0
        key.daily_hard_cap_usd = 10.0
        await db.commit()

        status = await check_budget_pre_request(db, key)
        await db.commit()
    assert status.soft_warning is True
    assert status.day_remaining == 5.0


# ── Bucket rollover ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_hourly_bucket_resets(fresh_key):
    """Bucket from 3 hours ago → reset to current hour, cost zeroed.
    Without this, an hourly cap that hit yesterday would never let
    the operator make another call."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey
    async with AsyncSessionLocal() as db:
        key = (await db.execute(select(ApiKey).where(ApiKey.id == "test-budget-key"))).scalar_one()
        # Bucket from 3 hours ago; cost was at the cap
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0, tzinfo=None)
        key.hour_bucket_ts = old
        key.hour_cost_usd = 5.0
        key.hourly_cap_usd = 5.0
        await db.commit()

        status = await check_budget_pre_request(db, key)  # should NOT raise
        await db.commit()
    assert status.hour_cost == 0.0  # bucket got reset


@pytest.mark.asyncio
async def test_stale_daily_bucket_resets(fresh_key):
    """Bucket from yesterday → reset, cost zeroed."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey
    async with AsyncSessionLocal() as db:
        key = (await db.execute(select(ApiKey).where(ApiKey.id == "test-budget-key"))).scalar_one()
        old = (datetime.now(timezone.utc) - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        key.day_bucket_ts = old
        key.day_cost_usd = 100.0
        key.daily_hard_cap_usd = 10.0
        await db.commit()

        status = await check_budget_pre_request(db, key)  # should NOT raise even though 100>10
        await db.commit()
    assert status.day_cost == 0.0


# ── record_cost increments correctly ───────────────────────────────────


@pytest.mark.asyncio
async def test_record_cost_increments_buckets(fresh_key):
    """After a successful request, record_cost adds to both day and
    hour buckets. Pre-set the bucket timestamps (record_cost doesn't
    reset; check_budget_pre_request does)."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey
    async with AsyncSessionLocal() as db:
        key = (await db.execute(select(ApiKey).where(ApiKey.id == "test-budget-key"))).scalar_one()
        now = datetime.now(timezone.utc)
        key.hour_bucket_ts = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
        key.day_bucket_ts = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        key.hour_cost_usd = 1.0
        key.day_cost_usd = 5.0
        await db.commit()

        await record_cost(db, key.id, 0.50)
        await db.commit()

        refreshed = (await db.execute(select(ApiKey).where(ApiKey.id == "test-budget-key"))).scalar_one()
    assert refreshed.hour_cost_usd == pytest.approx(1.50)
    assert refreshed.day_cost_usd == pytest.approx(5.50)


@pytest.mark.asyncio
async def test_record_cost_zero_is_noop(fresh_key):
    """Subscription providers report cost=0 (the actual money sits with
    Anthropic / OpenAI). record_cost with 0 must be a no-op so the
    counters don't drift on every subscription call."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey
    async with AsyncSessionLocal() as db:
        key = (await db.execute(select(ApiKey).where(ApiKey.id == "test-budget-key"))).scalar_one()
        key.hour_cost_usd = 1.0
        await db.commit()

        await record_cost(db, key.id, 0.0)
        await db.commit()

        refreshed = (await db.execute(select(ApiKey).where(ApiKey.id == "test-budget-key"))).scalar_one()
    assert refreshed.hour_cost_usd == 1.0  # unchanged
