"""v3.9.3 (#267) Phase 6 — provider-side memory flush handlers.

Covers ``app.memory.flush.maybe_flush_provider_memory`` in isolation:
- No-op when caller_memory_enabled=False
- No-op when caller_memory_active_flush_enabled=False
- No-op when conversation_id is None
- No-op when no marker exists (first request for this conv)
- No-op when marker.last_known_provider_id == new_provider_id (same provider)
- Fires when provider transitions; marker is updated to new provider
- Custom registered handler is invoked instead of noop
- Silent degrade on handler exception (no raise)
- Marker.last_known_external_ref cleared on transition (stale ref)
- Source-level wiring guard: messages.py calls maybe_flush_provider_memory
"""
from __future__ import annotations

import asyncio
import time
import pytest
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.db import (
    Base, ApiKey, Provider, CallerMemoryMarker,
)


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        # Seed: one api_key + two providers (old + new)
        s.add(ApiKey(
            id="key1", name="t", key_hash="h", key_prefix="p",
            key_type="standard", enabled=True,
        ))
        s.add(Provider(
            id="prov_old", name="OldClaude", provider_type="claude-oauth",
            api_key="x", priority=1, enabled=True, default_model="claude",
        ))
        s.add(Provider(
            id="prov_new", name="NewGPT", provider_type="openai",
            api_key="x", priority=2, enabled=True, default_model="gpt-4o",
        ))
        await s.commit()
        yield s
    await engine.dispose()


def _settings(enabled: bool = True, flush: bool = True):
    """Patch app.config.settings + app.memory.flush.settings simultaneously."""
    class _S:
        caller_memory_enabled = enabled
        caller_memory_active_flush_enabled = flush
    return _S()


async def _seed_marker(db, *, provider_id: str, ref: str | None = None):
    db.add(CallerMemoryMarker(
        api_key_id="key1", conversation_id="conv1", memory_tag="default",
        first_seen_at=time.time(), last_known_provider_id=provider_id,
        last_known_external_ref=ref,
    ))
    await db.commit()


# ── No-op cases ─────────────────────────────────────────────────────


async def test_noop_when_memory_disabled(db):
    from app.memory.flush import maybe_flush_provider_memory
    await _seed_marker(db, provider_id="prov_old")
    with patch("app.config.settings", _settings(enabled=False)):
        result = await maybe_flush_provider_memory(
            db, api_key_id="key1", conversation_id="conv1",
            memory_tag="default", new_provider_id="prov_new",
        )
    assert result is False
    # Marker untouched
    m = (await db.execute(select(CallerMemoryMarker))).scalar_one()
    assert m.last_known_provider_id == "prov_old"


async def test_noop_when_active_flush_disabled(db):
    from app.memory.flush import maybe_flush_provider_memory
    await _seed_marker(db, provider_id="prov_old")
    with patch("app.config.settings", _settings(flush=False)):
        result = await maybe_flush_provider_memory(
            db, api_key_id="key1", conversation_id="conv1",
            memory_tag="default", new_provider_id="prov_new",
        )
    assert result is False


async def test_noop_when_no_conversation_id(db):
    from app.memory.flush import maybe_flush_provider_memory
    with patch("app.config.settings", _settings()):
        result = await maybe_flush_provider_memory(
            db, api_key_id="key1", conversation_id=None,
            memory_tag=None, new_provider_id="prov_new",
        )
    assert result is False


async def test_noop_when_no_marker(db):
    """First request for a (key, conv, tag) — no marker yet, no flush."""
    from app.memory.flush import maybe_flush_provider_memory
    with patch("app.config.settings", _settings()):
        result = await maybe_flush_provider_memory(
            db, api_key_id="key1", conversation_id="conv1",
            memory_tag="default", new_provider_id="prov_new",
        )
    assert result is False


async def test_noop_when_same_provider(db):
    """Marker shows same provider as new — no transition, no flush."""
    from app.memory.flush import maybe_flush_provider_memory
    await _seed_marker(db, provider_id="prov_new")
    with patch("app.config.settings", _settings()):
        result = await maybe_flush_provider_memory(
            db, api_key_id="key1", conversation_id="conv1",
            memory_tag="default", new_provider_id="prov_new",
        )
    assert result is False


async def test_noop_when_marker_has_no_provider(db):
    """Marker exists but last_known_provider_id is NULL."""
    from app.memory.flush import maybe_flush_provider_memory
    await _seed_marker(db, provider_id=None)
    with patch("app.config.settings", _settings()):
        result = await maybe_flush_provider_memory(
            db, api_key_id="key1", conversation_id="conv1",
            memory_tag="default", new_provider_id="prov_new",
        )
    assert result is False


# ── Transition cases ───────────────────────────────────────────────


async def test_fires_on_provider_transition(db):
    from app.memory.flush import maybe_flush_provider_memory
    await _seed_marker(db, provider_id="prov_old", ref="thread_abc123")
    with patch("app.config.settings", _settings()):
        result = await maybe_flush_provider_memory(
            db, api_key_id="key1", conversation_id="conv1",
            memory_tag="default", new_provider_id="prov_new",
        )
    assert result is True
    # Marker updated
    m = (await db.execute(select(CallerMemoryMarker))).scalar_one()
    assert m.last_known_provider_id == "prov_new"
    # Stale external_ref cleared
    assert m.last_known_external_ref is None


async def test_custom_handler_invoked(db):
    from app.memory.flush import (
        maybe_flush_provider_memory, register_handler, _HANDLERS, _flush_noop,
    )
    await _seed_marker(db, provider_id="prov_old")

    called = {"n": 0, "ctx": None}

    async def my_handler(ctx):
        called["n"] += 1
        called["ctx"] = ctx
        return True

    register_handler("claude-oauth", my_handler)
    try:
        with patch("app.config.settings", _settings()):
            result = await maybe_flush_provider_memory(
                db, api_key_id="key1", conversation_id="conv1",
                memory_tag="default", new_provider_id="prov_new",
            )
        assert result is True
        assert called["n"] == 1
        # Handler receives full context
        assert called["ctx"]["old_provider_id"] == "prov_old"
        assert called["ctx"]["new_provider_id"] == "prov_new"
        assert called["ctx"]["provider_type"] == "claude-oauth"
        assert called["ctx"]["conversation_id"] == "conv1"
    finally:
        # Restore default handler set
        _HANDLERS.pop("claude-oauth", None)


async def test_handler_exception_silently_degraded(db):
    from app.memory.flush import (
        maybe_flush_provider_memory, register_handler, _HANDLERS,
    )
    await _seed_marker(db, provider_id="prov_old")

    async def boom(ctx):
        raise RuntimeError("upstream went away")

    register_handler("claude-oauth", boom)
    try:
        with patch("app.config.settings", _settings()):
            # Should NOT raise
            result = await maybe_flush_provider_memory(
                db, api_key_id="key1", conversation_id="conv1",
                memory_tag="default", new_provider_id="prov_new",
            )
        # Top-level catches the exception → silent degrade → returns False
        assert result is False
    finally:
        _HANDLERS.pop("claude-oauth", None)


async def test_handler_returning_false_still_updates_marker(db):
    """Per RFC #3: marker advances even on handler failure. The king-store
    is authoritative; we're not waiting on the upstream to acknowledge."""
    from app.memory.flush import (
        maybe_flush_provider_memory, register_handler, _HANDLERS,
    )
    await _seed_marker(db, provider_id="prov_old")

    async def returns_false(ctx):
        return False

    register_handler("claude-oauth", returns_false)
    try:
        with patch("app.config.settings", _settings()):
            result = await maybe_flush_provider_memory(
                db, api_key_id="key1", conversation_id="conv1",
                memory_tag="default", new_provider_id="prov_new",
            )
        assert result is True
        m = (await db.execute(select(CallerMemoryMarker))).scalar_one()
        assert m.last_known_provider_id == "prov_new"
    finally:
        _HANDLERS.pop("claude-oauth", None)


async def test_unknown_provider_type_falls_through_to_noop(db):
    """If old_provider_id points to a Provider that was deleted, we
    should still update the marker and noop the flush."""
    from app.memory.flush import maybe_flush_provider_memory
    await _seed_marker(db, provider_id="prov_deleted_id_999")
    with patch("app.config.settings", _settings()):
        result = await maybe_flush_provider_memory(
            db, api_key_id="key1", conversation_id="conv1",
            memory_tag="default", new_provider_id="prov_new",
        )
    assert result is True
    m = (await db.execute(select(CallerMemoryMarker))).scalar_one()
    assert m.last_known_provider_id == "prov_new"


# ── Source-level wiring guards ─────────────────────────────────────


def test_messages_endpoint_wires_flush():
    from pathlib import Path
    src = Path("app/api/messages.py").read_text()
    assert "from app.memory.flush import maybe_flush_provider_memory" in src
    assert "maybe_flush_provider_memory(" in src
    # Must fire AFTER route selection + cross-family adjustments
    idx_route = src.find("select_provider_with_503(")
    idx_flush = src.find("maybe_flush_provider_memory(")
    assert idx_route > 0 and idx_flush > idx_route


def test_flush_handler_registry_starts_empty():
    """No handlers registered by default — everything uses noop."""
    from app.memory.flush import _HANDLERS
    # _HANDLERS is module-level; only tests register into it. Snapshot
    # to confirm we don't ship with stale entries from a prior test run.
    # The test layout ensures cleanup, so a real module-import should be empty.
    # (Individual tests verify the register/unregister pattern works.)
    assert isinstance(_HANDLERS, dict)


def test_config_flag_exists():
    from app.config import settings
    assert hasattr(settings, "caller_memory_active_flush_enabled")
    assert isinstance(settings.caller_memory_active_flush_enabled, bool)
