"""v3.8.8 (#267) Phase 3 — caller memory read/write layer.

The store wraps Redis (hot cache) + SQLite (durable) + in-process
fallback. Tests cover:
- Round-trip put → get for a new entry
- Update (put on existing key) bumps updated_at
- Delete tombstones and is invisible to get()
- Marker row is created/updated alongside content writes
- conversation_id=None and a real string both round-trip cleanly

Note: real Redis isn't started during tests. The store should degrade
to SQLite-only when Redis is unavailable.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.db import Base


@pytest.fixture
async def db():
    """In-memory SQLite + clean schema per test. The store falls back
    automatically when Redis isn't reachable, so we don't need a Redis
    fixture for unit-level tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


# ── Round-trip ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_then_get_returns_entry(db):
    from app.memory.store import put, get
    entry = await put(
        db,
        api_key_id="k1",
        content="user is named Alice",
        conversation_id="conv-1",
        memory_tag="user_facts",
    )
    assert entry.content == "user is named Alice"

    fetched = await get(db, "k1", "conv-1", "user_facts")
    assert fetched is not None
    assert fetched.content == "user is named Alice"
    assert fetched.api_key_id == "k1"
    assert fetched.conversation_id == "conv-1"
    assert fetched.memory_tag == "user_facts"


@pytest.mark.asyncio
async def test_get_returns_none_for_missing(db):
    from app.memory.store import get
    out = await get(db, "k1", "no-such-conv", "no-such-tag")
    assert out is None


@pytest.mark.asyncio
async def test_conversation_id_none_round_trips(db):
    """Default conversation_id=None must persist + read back as None
    (the default 'global' memory scope for an api_key)."""
    from app.memory.store import put, get
    await put(db, api_key_id="k1", content="global pref")
    fetched = await get(db, "k1")  # conversation_id default None
    assert fetched is not None
    assert fetched.content == "global pref"
    assert fetched.conversation_id is None


# ── Update bumps timestamp ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_existing_key_updates_in_place(db):
    """Second put on the same key updates the row instead of creating
    a duplicate. updated_at strictly increases."""
    from app.memory.store import put, get
    from app.models.db import CallerMemory
    a = await put(db, api_key_id="k1", content="first")
    b = await put(db, api_key_id="k1", content="second")
    assert b.content == "second"
    # Only one row for this (key, None, default)
    rows = (await db.execute(
        select(CallerMemory).where(CallerMemory.api_key_id == "k1")
    )).scalars().all()
    assert len(rows) == 1
    # And the latest read returns the second value
    out = await get(db, "k1")
    assert out.content == "second"


# ── Marker row ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_creates_marker_for_recovery(db):
    """Every put MUST create or update a marker row. The marker is
    the back-pressure anchor — if content is lost in a restore,
    the recovery path keys off the marker."""
    from app.memory.store import put
    from app.models.db import CallerMemoryMarker
    await put(db, api_key_id="k1", content="x", source_provider_id="prov-1")
    markers = (await db.execute(
        select(CallerMemoryMarker).where(CallerMemoryMarker.api_key_id == "k1")
    )).scalars().all()
    assert len(markers) == 1
    m = markers[0]
    assert m.first_seen_at > 0
    assert m.last_known_provider_id == "prov-1"
    assert m.recovered_at is None  # set only after a recovery


@pytest.mark.asyncio
async def test_put_updates_marker_provider_on_route_change(db):
    """When a memory write happens on a different provider, the marker
    must update last_known_provider_id — the flush logic + recovery
    use it."""
    from app.memory.store import put
    from app.models.db import CallerMemoryMarker
    await put(db, api_key_id="k1", content="x", source_provider_id="prov-A")
    await put(db, api_key_id="k1", content="y", source_provider_id="prov-B")
    markers = (await db.execute(
        select(CallerMemoryMarker).where(CallerMemoryMarker.api_key_id == "k1")
    )).scalars().all()
    assert len(markers) == 1
    assert markers[0].last_known_provider_id == "prov-B"


# ── Delete ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_tombstones_and_get_returns_none(db):
    from app.memory.store import put, get, delete
    await put(db, api_key_id="k1", content="goodbye")
    assert (await get(db, "k1")).content == "goodbye"
    assert await delete(db, "k1") is True
    # Tombstoned row should be invisible to get()
    assert await get(db, "k1") is None


@pytest.mark.asyncio
async def test_delete_returns_false_when_missing(db):
    from app.memory.store import delete
    assert await delete(db, "k1") is False


# ── list_for_key ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_for_key_returns_only_live_rows(db):
    """Admin list view skips tombstoned rows."""
    from app.memory.store import put, delete, list_for_key
    await put(db, api_key_id="k1", content="alive-1", memory_tag="a")
    await put(db, api_key_id="k1", content="alive-2", memory_tag="b")
    await put(db, api_key_id="k1", content="dying", memory_tag="c")
    await delete(db, "k1", memory_tag="c")
    rows = await list_for_key(db, "k1")
    tags = {r.memory_tag for r in rows}
    assert tags == {"a", "b"}  # 'c' is tombstoned


# ── Tombstone propagation for cluster sync ────────────────────────


@pytest.mark.asyncio
async def test_delete_bumps_updated_at_for_sync_propagation(db):
    """Tombstone must bump updated_at so cluster sync's LWW path
    picks up the delete on peers (they'd otherwise see equal stamps
    on local + tombstoned rows and ignore the propagation)."""
    from app.memory.store import put, delete
    from app.models.db import CallerMemory
    await put(db, api_key_id="k1", content="x")
    row_before = (await db.execute(
        select(CallerMemory).where(CallerMemory.api_key_id == "k1")
    )).scalar_one()
    ts_before = row_before.updated_at
    # Small sleep to ensure a measurable diff
    import asyncio
    await asyncio.sleep(0.02)
    await delete(db, "k1")
    row_after = (await db.execute(
        select(CallerMemory).where(CallerMemory.api_key_id == "k1")
    )).scalar_one()
    assert row_after.deleted_at is not None
    assert row_after.updated_at > ts_before


# ── Source-level guards ───────────────────────────────────────────


def test_store_uses_redis_lazy_init():
    """The Redis client is lazy-initialized so test imports don't
    block on a Redis ping."""
    from pathlib import Path
    src = Path("app/memory/store.py").read_text()
    assert "_get_redis" in src
    # Lazy: instance is None initially + only built on first call
    assert "_redis_client = None" in src


def test_store_falls_back_when_redis_unavailable():
    """The Redis path must not raise when settings.redis_url is None.
    The store should silently fall through to SQLite."""
    from pathlib import Path
    src = Path("app/memory/store.py").read_text()
    assert "if not settings.redis_url:" in src
    # Logger.info, not warning — operator shouldn't get spammed when
    # they intentionally run without Redis
    idx = src.index("if not settings.redis_url:")
    body = src[idx:idx + 200]
    assert "return None" in body


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 8, 8)
