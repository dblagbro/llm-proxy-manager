"""Tests for the v3.2.11 auto-stamp event listener.

The listener auto-bumps ``Provider.last_user_edit_at`` whenever a
"user-meaningful" column changes during an UPDATE. Background
mutations (OAuth token refresh, deleted_at tombstone, updated_at
itself) must NOT trigger a bump — that would defeat the v3.0.11
design which exists specifically to distinguish admin edits from
background machinery.

This is the safety-net for direct DB writes; the v3.2.7 cluster-sync
fix made the LWW comparison correct, this stops the bug from being
generated in the first place.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture
async def fresh_provider():
    """Insert a Provider row with a known last_user_edit_at; yield the
    AsyncSessionLocal so tests can run their own transactions."""
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, Provider

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(Provider).where(Provider.id == "stamp-test"))
        await cleanup.commit()
    async with AsyncSessionLocal() as db:
        p = Provider(
            id="stamp-test",
            name="orig-name",
            provider_type="openai",
            api_key="orig-key",
            priority=10,
            enabled=True,
            extra_config={"foo": "orig"},
            last_user_edit_at=1000.0,
        )
        db.add(p)
        await db.commit()

    yield AsyncSessionLocal

    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(Provider).where(Provider.id == "stamp-test"))
        await cleanup.commit()


# ── Bumps on user-meaningful changes ─────────────────────────────────


@pytest.mark.asyncio
async def test_rename_bumps_stamp(fresh_provider):
    """Renaming a Provider is the canonical user-meaningful change."""
    from app.models.db import Provider
    AsyncSessionLocal = fresh_provider
    before = time.time()
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
        assert p.last_user_edit_at == 1000.0
        p.name = "new-name"
        await db.commit()
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
    assert p.last_user_edit_at >= before, "rename must bump last_user_edit_at"
    assert p.last_user_edit_at != 1000.0


@pytest.mark.asyncio
async def test_priority_change_bumps_stamp(fresh_provider):
    """Priority shuffles are admin actions; must bump."""
    from app.models.db import Provider
    AsyncSessionLocal = fresh_provider
    before = time.time()
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
        p.priority = 5
        await db.commit()
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
    assert p.last_user_edit_at >= before


@pytest.mark.asyncio
async def test_extra_config_change_bumps_stamp(fresh_provider):
    """The v3.2.7 trigger case: extra_config edit (e.g. bridge_url) is
    a real admin change and must propagate via the user-edit LWW path."""
    from app.models.db import Provider
    AsyncSessionLocal = fresh_provider
    before = time.time()
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
        p.extra_config = {"foo": "edited", "bridge_url": "https://example/bridge"}
        await db.commit()
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
    assert p.last_user_edit_at >= before


# ── Does NOT bump on background changes ──────────────────────────────


@pytest.mark.asyncio
async def test_api_key_rotation_does_not_bump(fresh_provider):
    """OAuth refresh / admin re-keying via the dedicated endpoint
    bumps via _stamp_user_edit; a bare api_key write is treated as a
    background rotation and must NOT bump last_user_edit_at."""
    from app.models.db import Provider
    AsyncSessionLocal = fresh_provider
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
        p.api_key = "rotated-token"
        await db.commit()
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
    assert p.last_user_edit_at == 1000.0, "api_key alone must not bump stamp"


@pytest.mark.asyncio
async def test_oauth_refresh_fields_do_not_bump(fresh_provider):
    """oauth_refresh_token + oauth_expires_at rotate on every refresh;
    they're pure background machinery and the whole point of the
    user-edit timestamp is to ignore them."""
    from app.models.db import Provider
    AsyncSessionLocal = fresh_provider
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
        p.oauth_refresh_token = "new-rt"
        p.oauth_expires_at = time.time() + 3600
        await db.commit()
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
    assert p.last_user_edit_at == 1000.0


@pytest.mark.asyncio
async def test_explicit_stamp_is_respected(fresh_provider):
    """If caller explicitly sets last_user_edit_at (e.g. data import
    preserving historical timestamps), the listener must NOT clobber
    that with the current wall-clock value."""
    from app.models.db import Provider
    AsyncSessionLocal = fresh_provider
    historic = 12345.0
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
        p.name = "imported"
        p.last_user_edit_at = historic
        await db.commit()
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
    assert p.last_user_edit_at == historic, "explicit caller stamp must win"


@pytest.mark.asyncio
async def test_no_change_no_bump(fresh_provider):
    """A flush with no actual column changes shouldn't fire the
    listener at all (SQLAlchemy skips the UPDATE, but defensively
    verify the stamp is preserved)."""
    from app.models.db import Provider
    AsyncSessionLocal = fresh_provider
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
        # Touch nothing; just commit
        await db.commit()
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(Provider).where(Provider.id == "stamp-test"))).scalar_one()
    assert p.last_user_edit_at == 1000.0
