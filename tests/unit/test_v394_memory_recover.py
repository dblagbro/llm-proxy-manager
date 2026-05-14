"""v3.9.4 (#267) Phase 7 — back-pressure memory recovery.

Covers ``app.memory.recover.maybe_recover_memory`` in isolation, plus
its wiring into ``app.memory.inject.maybe_inject_memory``.

- No-op when caller_memory_enabled=False
- No-op when caller_memory_recovery_enabled=False
- No-op when conversation_id is None
- No-op when no marker exists
- No-op when marker.recovered_at is already set
- No-op when marker.last_known_provider_id is null
- No-op when no handler registered for provider_type
- Successful recovery: persists content via store.put(), sets marker.recovered_at
- Handler returning None: marker NOT advanced (retry-able)
- Silent degrade on handler exception
- Wiring guard: inject.py calls maybe_recover_memory when get() returns None
"""
from __future__ import annotations

import time
import pytest
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.db import (
    Base, ApiKey, Provider, CallerMemory, CallerMemoryMarker,
)


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        s.add(ApiKey(
            id="key1", name="t", key_hash="h", key_prefix="p",
            key_type="standard", enabled=True,
        ))
        s.add(Provider(
            id="prov_old", name="OldClaude", provider_type="claude-oauth",
            api_key="x", priority=1, enabled=True, default_model="claude",
        ))
        await s.commit()
        yield s
    await engine.dispose()


def _settings(enabled: bool = True, recovery: bool = True):
    class _S:
        caller_memory_enabled = enabled
        caller_memory_recovery_enabled = recovery
        # store.put() reads cluster_node_id and redis_url; we want neither
        cluster_node_id = "test-node"
        redis_url = None
    return _S()


async def _seed_marker(db, *, provider_id: str | None = "prov_old",
                       ref: str | None = "thread_abc",
                       recovered_at: float | None = None):
    db.add(CallerMemoryMarker(
        api_key_id="key1", conversation_id="conv1", memory_tag="default",
        first_seen_at=time.time(), last_known_provider_id=provider_id,
        last_known_external_ref=ref, recovered_at=recovered_at,
    ))
    await db.commit()


# ── No-op cases ─────────────────────────────────────────────────────


async def test_noop_when_memory_disabled(db):
    from app.memory.recover import maybe_recover_memory
    await _seed_marker(db)
    with patch("app.config.settings", _settings(enabled=False)):
        result = await maybe_recover_memory(
            db, api_key_id="key1", conversation_id="conv1", memory_tag="default",
        )
    assert result is None


async def test_noop_when_recovery_disabled(db):
    from app.memory.recover import maybe_recover_memory
    await _seed_marker(db)
    with patch("app.config.settings", _settings(recovery=False)):
        result = await maybe_recover_memory(
            db, api_key_id="key1", conversation_id="conv1", memory_tag="default",
        )
    assert result is None


async def test_noop_when_no_conversation(db):
    from app.memory.recover import maybe_recover_memory
    with patch("app.config.settings", _settings()):
        result = await maybe_recover_memory(
            db, api_key_id="key1", conversation_id=None, memory_tag=None,
        )
    assert result is None


async def test_noop_when_no_marker(db):
    from app.memory.recover import maybe_recover_memory
    with patch("app.config.settings", _settings()):
        result = await maybe_recover_memory(
            db, api_key_id="key1", conversation_id="conv1", memory_tag="default",
        )
    assert result is None


async def test_noop_when_already_recovered(db):
    """Marker.recovered_at is set — skip to avoid infinite retry loops."""
    from app.memory.recover import maybe_recover_memory
    from app.memory.recover import register_handler, _HANDLERS
    await _seed_marker(db, recovered_at=time.time())

    called = {"n": 0}
    async def my_handler(ctx):
        called["n"] += 1
        return "content"

    register_handler("claude-oauth", my_handler)
    try:
        with patch("app.config.settings", _settings()):
            result = await maybe_recover_memory(
                db, api_key_id="key1", conversation_id="conv1", memory_tag="default",
            )
        assert result is None
        assert called["n"] == 0  # handler never invoked
    finally:
        _HANDLERS.pop("claude-oauth", None)


async def test_noop_when_no_last_known_provider(db):
    from app.memory.recover import maybe_recover_memory
    await _seed_marker(db, provider_id=None)
    with patch("app.config.settings", _settings()):
        result = await maybe_recover_memory(
            db, api_key_id="key1", conversation_id="conv1", memory_tag="default",
        )
    assert result is None


async def test_noop_when_no_handler_registered(db):
    """Steady-state today: empty registry → no-op (cheap)."""
    from app.memory.recover import maybe_recover_memory, _HANDLERS
    await _seed_marker(db)
    # Ensure clean registry
    _HANDLERS.clear()
    with patch("app.config.settings", _settings()):
        result = await maybe_recover_memory(
            db, api_key_id="key1", conversation_id="conv1", memory_tag="default",
        )
    assert result is None
    # Marker NOT updated — we want a future handler to pick this up
    m = (await db.execute(select(CallerMemoryMarker))).scalar_one()
    assert m.recovered_at is None


# ── Recovery cases ───────────────────────────────────────────────


async def test_successful_recovery_persists_content(db):
    from app.memory.recover import (
        maybe_recover_memory, register_handler, _HANDLERS,
    )
    await _seed_marker(db, ref="thread_xyz")

    async def my_handler(ctx):
        # Handler sees full context
        assert ctx["api_key_id"] == "key1"
        assert ctx["conversation_id"] == "conv1"
        assert ctx["memory_tag"] == "default"
        assert ctx["old_provider_id"] == "prov_old"
        assert ctx["provider_type"] == "claude-oauth"
        assert ctx["last_known_external_ref"] == "thread_xyz"
        return "reconstructed memory blob"

    register_handler("claude-oauth", my_handler)
    try:
        with patch("app.config.settings", _settings()):
            result = await maybe_recover_memory(
                db, api_key_id="key1", conversation_id="conv1", memory_tag="default",
            )
        assert result == "reconstructed memory blob"
        # Content persisted
        cm = (await db.execute(select(CallerMemory))).scalar_one()
        assert cm.content == "reconstructed memory blob"
        assert cm.source_provider_id == "prov_old"
        # Marker advanced
        m = (await db.execute(select(CallerMemoryMarker))).scalar_one()
        assert m.recovered_at is not None
    finally:
        _HANDLERS.pop("claude-oauth", None)


async def test_handler_returning_none_does_not_advance_marker(db):
    """Failed recovery should NOT set recovered_at — must retry."""
    from app.memory.recover import (
        maybe_recover_memory, register_handler, _HANDLERS,
    )
    await _seed_marker(db)

    async def my_handler(ctx):
        return None  # upstream gave us nothing

    register_handler("claude-oauth", my_handler)
    try:
        with patch("app.config.settings", _settings()):
            result = await maybe_recover_memory(
                db, api_key_id="key1", conversation_id="conv1", memory_tag="default",
            )
        assert result is None
        m = (await db.execute(select(CallerMemoryMarker))).scalar_one()
        assert m.recovered_at is None
    finally:
        _HANDLERS.pop("claude-oauth", None)


async def test_handler_exception_silently_degraded(db):
    from app.memory.recover import (
        maybe_recover_memory, register_handler, _HANDLERS,
    )
    await _seed_marker(db)

    async def boom(ctx):
        raise RuntimeError("upstream died")

    register_handler("claude-oauth", boom)
    try:
        with patch("app.config.settings", _settings()):
            # Must not raise
            result = await maybe_recover_memory(
                db, api_key_id="key1", conversation_id="conv1", memory_tag="default",
            )
        assert result is None
        # Marker NOT advanced — retry-able
        m = (await db.execute(select(CallerMemoryMarker))).scalar_one()
        assert m.recovered_at is None
    finally:
        _HANDLERS.pop("claude-oauth", None)


async def test_handler_returning_empty_string_treated_as_failure(db):
    """Empty string is not useful content; treat like None."""
    from app.memory.recover import (
        maybe_recover_memory, register_handler, _HANDLERS,
    )
    await _seed_marker(db)

    async def my_handler(ctx):
        return ""

    register_handler("claude-oauth", my_handler)
    try:
        with patch("app.config.settings", _settings()):
            result = await maybe_recover_memory(
                db, api_key_id="key1", conversation_id="conv1", memory_tag="default",
            )
        assert result is None
        m = (await db.execute(select(CallerMemoryMarker))).scalar_one()
        assert m.recovered_at is None
    finally:
        _HANDLERS.pop("claude-oauth", None)


# ── Source-level wiring guards ─────────────────────────────────────


def test_inject_wires_recovery():
    from pathlib import Path
    src = Path("app/memory/inject.py").read_text()
    assert "from app.memory.recover import maybe_recover_memory" in src
    assert "maybe_recover_memory(" in src
    # Must fire AFTER get() returns None
    idx_get = src.find("entry = await get(")
    idx_recover = src.find("maybe_recover_memory(")
    assert idx_get > 0 and idx_recover > idx_get


def test_recovery_module_docstring_acknowledges_noop():
    from pathlib import Path
    src = Path("app/memory/recover.py").read_text()
    # Documentation honest about noop handlers today
    assert "noop" in src.lower()
    assert "Phase 7" in src


def test_config_flag_exists():
    from app.config import settings
    assert hasattr(settings, "caller_memory_recovery_enabled")
    assert isinstance(settings.caller_memory_recovery_enabled, bool)


def test_runtime_schema_exposes_recovery_flag():
    from app.config_runtime import SCHEMA
    assert "caller_memory_recovery_enabled" in SCHEMA
    assert SCHEMA["caller_memory_recovery_enabled"]["type"] == "bool"
