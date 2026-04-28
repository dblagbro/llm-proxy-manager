"""v2.8.2: priority auto-bump on insert/update — chain-reaction shifts."""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
UTC = timezone.utc

import pytest


# Stub heavy deps before app imports
def _stub():
    if "litellm" not in sys.modules:
        m = types.ModuleType("litellm")
        m.RateLimitError = type("RateLimitError", (Exception,), {})
        sys.modules["litellm"] = m
    if "prometheus_client" not in sys.modules:
        class _Noop:
            def __init__(self, *a, **kw): pass
            def __call__(self, *a, **kw): return self
            def labels(self, *a, **kw): return self
            def inc(self, *a, **kw): pass
            def observe(self, *a, **kw): pass
            def set(self, *a, **kw): pass
            def info(self, *a, **kw): pass
        m = types.ModuleType("prometheus_client")
        m.CONTENT_TYPE_LATEST = "text/plain"
        m.Counter = m.Gauge = m.Histogram = m.Info = _Noop
        m.generate_latest = lambda: b""
        sys.modules["prometheus_client"] = m
_stub()


from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select
from app.models.db import Base, Provider
from app.api.providers import _bump_priority_conflicts


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


def _make(name: str, priority: int, *, created_offset_s: int = 0) -> Provider:
    return Provider(
        id=name,
        name=name,
        provider_type="anthropic",
        api_key="x",
        priority=priority,
        enabled=True,
        timeout_sec=30,
        exclude_from_tool_requests=False,
        extra_config={},
        created_at=datetime.now(UTC).replace(microsecond=0).fromtimestamp(
            1000000 + created_offset_s, UTC
        ),
    )


class TestBumpPriorityConflicts:
    @pytest.mark.asyncio
    async def test_no_conflict_no_bump(self, db):
        db.add_all([_make("a", 1), _make("b", 2)])
        await db.commit()
        n = await _bump_priority_conflicts(db, 5)
        await db.commit()
        assert n == 0
        rows = (await db.execute(select(Provider).order_by(Provider.priority))).scalars().all()
        assert [r.priority for r in rows] == [1, 2]

    @pytest.mark.asyncio
    async def test_single_collision_bumps_one(self, db):
        # Existing: a=1, b=2. New row will take 2 → b should bump to 3.
        db.add_all([_make("a", 1), _make("b", 2)])
        await db.commit()
        n = await _bump_priority_conflicts(db, 2)
        await db.commit()
        assert n == 1
        by_name = {r.name: r.priority for r in
                    (await db.execute(select(Provider))).scalars().all()}
        assert by_name == {"a": 1, "b": 3}

    @pytest.mark.asyncio
    async def test_chain_reaction(self, db):
        # Existing: 1..6. New row will take 2 → 2→3, 3→4, 4→5, 5→6, 6→7.
        for i, name in enumerate(["a", "b", "c", "d", "e", "f"], start=1):
            db.add(_make(name, i, created_offset_s=i))
        await db.commit()
        n = await _bump_priority_conflicts(db, 2)
        await db.commit()
        assert n == 5  # b, c, d, e, f all bumped
        by_name = {r.name: r.priority for r in
                    (await db.execute(select(Provider))).scalars().all()}
        # The new row would slot into 2 — caller does that. Bumping leaves
        # priorities {a:1, b:3, c:4, d:5, e:6, f:7} with slot 2 unoccupied.
        assert by_name == {"a": 1, "b": 3, "c": 4, "d": 5, "e": 6, "f": 7}

    @pytest.mark.asyncio
    async def test_chain_with_gap(self, db):
        # Existing: 1, 3, 4, 7. New row asks for 3 → 3→4, 4→5. Gap at 7 stops it.
        for name, p in [("a", 1), ("b", 3), ("c", 4), ("d", 7)]:
            db.add(_make(name, p, created_offset_s=p))
        await db.commit()
        n = await _bump_priority_conflicts(db, 3)
        await db.commit()
        assert n == 2  # b → 4, c → 5
        by_name = {r.name: r.priority for r in
                    (await db.execute(select(Provider))).scalars().all()}
        # b bumped to 4, c bumped to 5; d stays at 7 (no chain-reaction past 5)
        assert by_name == {"a": 1, "b": 4, "c": 5, "d": 7}

    @pytest.mark.asyncio
    async def test_exclude_self_for_update_path(self, db):
        # When PUT changes b's priority from 5 to 2, b should NOT be bumped
        # by its own request — exclude_id=b.
        db.add_all([_make("a", 1), _make("b", 5), _make("c", 2, created_offset_s=10)])
        await db.commit()
        n = await _bump_priority_conflicts(db, 2, exclude_id="b")
        await db.commit()
        assert n == 1  # only c
        by_name = {r.name: r.priority for r in
                    (await db.execute(select(Provider))).scalars().all()}
        # c bumped 2→3; b unchanged here (caller will then set b.priority=2)
        assert by_name == {"a": 1, "b": 5, "c": 3}

    @pytest.mark.asyncio
    async def test_two_existing_at_same_priority_both_bump_in_lockstep(self, db):
        # Defensive: pre-existing duplicates at the same priority both bump
        # (and chain-react). a=1, b=2, c=2 (duplicate). New asks 2 → both b
        # and c bump to 3 in one sweep.
        db.add_all([
            _make("a", 1),
            _make("b", 2, created_offset_s=10),
            _make("c", 2, created_offset_s=20),
        ])
        await db.commit()
        n = await _bump_priority_conflicts(db, 2)
        await db.commit()
        assert n == 2
        by_name = {r.name: r.priority for r in
                    (await db.execute(select(Provider))).scalars().all()}
        assert by_name == {"a": 1, "b": 3, "c": 3}
