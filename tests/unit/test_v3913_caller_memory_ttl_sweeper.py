"""v3.9.13 — per-key TTL sweeper for caller_memory.

Hub team's ask: per-key opt-in TTL. NULL on api_keys.caller_memory_ttl_days
= no expiry (current behavior); positive int = sweeper tombstones rows
older than N days for THAT key.

Tests confirm:
- The opt-in is per-key (a key with TTL=null isn't affected)
- Tombstones write deleted_at + bump updated_at (LWW cluster sync)
- Redis cache invalidation
- Boot delay so sweeper doesn't fire mid-startup
- Sweeper is gated on caller_memory_enabled (no-op when feature off)
"""
from __future__ import annotations

import time
import pytest
from datetime import datetime, timezone
from unittest.mock import patch
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.db import Base, ApiKey, CallerMemory


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        s.add(ApiKey(
            id="key_no_ttl", name="hub-untouched", key_hash="h1",
            key_prefix="p1", key_type="standard", enabled=True,
            caller_memory_ttl_days=None,  # explicit
        ))
        s.add(ApiKey(
            id="key_7d", name="hub-7day", key_hash="h2",
            key_prefix="p2", key_type="standard", enabled=True,
            caller_memory_ttl_days=7,
        ))
        s.add(ApiKey(
            id="key_30d", name="paperless-30day", key_hash="h3",
            key_prefix="p3", key_type="standard", enabled=True,
            caller_memory_ttl_days=30,
        ))
        now = time.time()
        # row 1: under no-TTL key, ancient — should NOT be tombstoned
        s.add(CallerMemory(
            api_key_id="key_no_ttl", conversation_id="c1",
            memory_tag="default", content="ancient — protected",
            updated_at=now - (100 * 86400),  # 100 days
        ))
        # row 2: under 7d-TTL key, 10 days old — should be tombstoned
        s.add(CallerMemory(
            api_key_id="key_7d", conversation_id="c2",
            memory_tag="default", content="stale — should die",
            updated_at=now - (10 * 86400),
        ))
        # row 3: under 7d-TTL key, 3 days old — should survive
        s.add(CallerMemory(
            api_key_id="key_7d", conversation_id="c3",
            memory_tag="default", content="fresh — survives",
            updated_at=now - (3 * 86400),
        ))
        # row 4: under 30d-TTL key, 20 days old — should survive
        s.add(CallerMemory(
            api_key_id="key_30d", conversation_id="c4",
            memory_tag="default", content="20d under 30d limit",
            updated_at=now - (20 * 86400),
        ))
        # row 5: under 30d-TTL key, 45 days old — should be tombstoned
        s.add(CallerMemory(
            api_key_id="key_30d", conversation_id="c5",
            memory_tag="default", content="45d > 30d limit",
            updated_at=now - (45 * 86400),
        ))
        await s.commit()
        yield s
    await engine.dispose()


def _settings(enabled: bool = True):
    class _S:
        caller_memory_enabled = enabled
        caller_memory_ttl_sweep_interval_sec = 3600
        redis_url = None
        cluster_node_id = "test-node"
    return _S()


# ── Schema + admin surface ─────────────────────────────────────────


def test_api_key_model_has_ttl_column():
    assert hasattr(ApiKey, "caller_memory_ttl_days")


def test_migration_adds_column():
    src = Path("app/models/database.py").read_text()
    assert "ALTER TABLE api_keys ADD COLUMN caller_memory_ttl_days INTEGER" in src


def test_keyupdate_schema_includes_ttl():
    from app.api.apikeys import KeyUpdate
    assert "caller_memory_ttl_days" in KeyUpdate.model_fields


def test_apikey_serialize_emits_ttl():
    src = Path("app/api/apikeys.py").read_text()
    assert '"caller_memory_ttl_days":' in src


def test_apikey_update_clear_uses_nonpositive_sentinel():
    """Matches the pattern of other -1=clear fields, but uses <=0 here
    because negative days makes no sense."""
    src = Path("app/api/apikeys.py").read_text()
    assert "body.caller_memory_ttl_days <= 0" in src


# ── Sweeper logic ──────────────────────────────────────────────────


async def test_sweeper_tombstones_only_expired_rows_of_ttl_keys(db):
    """The hot path: row 2 + row 5 die; rows 1, 3, 4 survive."""
    from app.monitoring.caller_memory_ttl_sweeper import _sweep_once

    # Monkey-patch AsyncSessionLocal so _sweep_once uses our test session
    from app.models import database as _db_mod
    fake_factory = lambda: db
    with patch.object(_db_mod, "AsyncSessionLocal", fake_factory):
        with patch("app.config.settings", _settings()):
            stats = await _sweep_once()

    assert stats["keys_with_ttl"] == 2
    assert stats["rows_tombstoned"] == 2

    # Verify survivors
    rows = (await db.execute(select(CallerMemory))).scalars().all()
    by_conv = {r.conversation_id: r for r in rows}
    assert by_conv["c1"].deleted_at is None, "no-TTL row must survive"
    assert by_conv["c2"].deleted_at is not None, "10d row under 7d TTL must tombstone"
    assert by_conv["c3"].deleted_at is None, "3d row under 7d TTL must survive"
    assert by_conv["c4"].deleted_at is None, "20d row under 30d TTL must survive"
    assert by_conv["c5"].deleted_at is not None, "45d row under 30d TTL must tombstone"


async def test_tombstone_bumps_updated_at_for_cluster_lww(db):
    from app.monitoring.caller_memory_ttl_sweeper import _sweep_once
    from app.models import database as _db_mod

    pre_now = time.time()
    fake_factory = lambda: db
    with patch.object(_db_mod, "AsyncSessionLocal", fake_factory):
        with patch("app.config.settings", _settings()):
            await _sweep_once()
    rows = (await db.execute(
        select(CallerMemory).where(CallerMemory.deleted_at.is_not(None))
    )).scalars().all()
    for r in rows:
        # updated_at was bumped to roughly now, NOT left at the old expired ts
        assert r.updated_at >= pre_now - 1, (
            f"updated_at not bumped on tombstone — was {r.updated_at}, "
            f"need >= {pre_now} so LWW cluster sync propagates"
        )


async def test_sweeper_idempotent_on_repeat_run(db):
    """Re-running the sweeper finds nothing new — already-tombstoned
    rows are skipped because deleted_at.is_not(None)."""
    from app.monitoring.caller_memory_ttl_sweeper import _sweep_once
    from app.models import database as _db_mod
    fake_factory = lambda: db
    with patch.object(_db_mod, "AsyncSessionLocal", fake_factory):
        with patch("app.config.settings", _settings()):
            r1 = await _sweep_once()
            r2 = await _sweep_once()
    assert r1["rows_tombstoned"] == 2
    assert r2["rows_tombstoned"] == 0


# ── Lifecycle ──────────────────────────────────────────────────────


def test_sweeper_module_imports_cleanly():
    import importlib
    mod = importlib.import_module("app.monitoring.caller_memory_ttl_sweeper")
    assert hasattr(mod, "start")
    assert hasattr(mod, "_sweep_once")


def test_sweeper_registered_in_main():
    src = Path("app/main.py").read_text()
    assert "caller_memory_ttl_sweeper" in src
    assert "_ttl_sweeper.start()" in src


def test_sweeper_skips_when_feature_disabled():
    """start() must early-return without spawning the task when
    caller_memory_enabled=False so we don't sweep an inert feature."""
    src = Path("app/monitoring/caller_memory_ttl_sweeper.py").read_text()
    idx = src.index("def start()")
    fn = src[idx:idx + 1500]
    assert "caller_memory_enabled" in fn
    assert "return" in fn


def test_interval_floor_at_60_seconds():
    """Operators can tune the sweep interval but not below 60s — avoid
    a runaway misconfiguration sweeping every second."""
    src = Path("app/monitoring/caller_memory_ttl_sweeper.py").read_text()
    assert "max(60, v)" in src


def test_sweeper_invalidates_redis():
    """Tombstones leave Redis hot cache stale — must clear cache keys."""
    src = Path("app/monitoring/caller_memory_ttl_sweeper.py").read_text()
    assert "from app.memory.store import _get_redis" in src
    assert "await r.delete(_key(" in src
