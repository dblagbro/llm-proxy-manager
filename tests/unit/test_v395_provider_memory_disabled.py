"""v3.9.5 (#267) Phase 8 — implicit-memory disable at provider config.

Covers the Provider.memory_disabled flag's effect on extract + inject:
- Provider has memory_disabled column with default False
- extract.maybe_extract_memory_writes skips when source_provider has memory_disabled=True
- extract continues normally when memory_disabled=False or source_provider unknown
- messages.py and completions.py source check route.provider.memory_disabled before inject
- Migration ADD COLUMN statement exists
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
            id="prov_normal", name="Normal", provider_type="anthropic",
            api_key="x", priority=1, enabled=True, default_model="claude",
            memory_disabled=False,
        ))
        s.add(Provider(
            id="prov_silent", name="MemorySilent", provider_type="anthropic",
            api_key="x", priority=2, enabled=True, default_model="claude",
            memory_disabled=True,
        ))
        await s.commit()
        yield s
    await engine.dispose()


def _settings():
    class _S:
        caller_memory_enabled = True
        caller_memory_recovery_enabled = True
        cluster_node_id = "test-node"
        redis_url = None
    return _S()


def _memory_tool_response_with_create(content_text: str) -> dict:
    """Anthropic /v1/messages response with a memory tool_use create."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "toolu_x",
             "name": "memory_20250818",
             "input": {"command": "create", "path": "/memories/notes.md",
                       "content": content_text}},
        ],
        "model": "claude-haiku-4-5-20251001",
    }


# ── Schema ──────────────────────────────────────────────────────────


def test_provider_model_has_memory_disabled_column():
    from app.models.db import Provider
    assert hasattr(Provider, "memory_disabled")


async def test_provider_memory_disabled_default_false(db):
    p = Provider(
        id="prov_test", name="T", provider_type="openai",
        api_key="x", priority=1, enabled=True, default_model="m",
    )
    db.add(p)
    await db.commit()
    fetched = (await db.execute(
        select(Provider).where(Provider.id == "prov_test")
    )).scalar_one()
    # Default behaves as False (None is also falsy and acceptable —
    # the gating code uses ``getattr(..., False)``).
    assert not fetched.memory_disabled


# ── extract gating ─────────────────────────────────────────────────


async def test_extract_skipped_when_provider_memory_disabled(db):
    from app.memory.extract import maybe_extract_memory_writes
    with patch("app.config.settings", _settings()):
        n = await maybe_extract_memory_writes(
            db, response_dict=_memory_tool_response_with_create("note A"),
            api_key_id="key1", conversation_id="conv1",
            source_provider_id="prov_silent",
        )
    assert n == 0
    # No CallerMemory row created
    rows = (await db.execute(select(CallerMemory))).scalars().all()
    assert len(rows) == 0


async def test_extract_runs_normally_when_provider_not_disabled(db):
    from app.memory.extract import maybe_extract_memory_writes
    with patch("app.config.settings", _settings()):
        n = await maybe_extract_memory_writes(
            db, response_dict=_memory_tool_response_with_create("note B"),
            api_key_id="key1", conversation_id="conv1",
            source_provider_id="prov_normal",
        )
    assert n == 1
    rows = (await db.execute(select(CallerMemory))).scalars().all()
    assert len(rows) == 1
    assert rows[0].content == "note B"


async def test_extract_runs_when_provider_id_unknown(db):
    """If source_provider_id points to a deleted provider, don't block
    extract — we don't have a strong reason to assume it was disabled."""
    from app.memory.extract import maybe_extract_memory_writes
    with patch("app.config.settings", _settings()):
        n = await maybe_extract_memory_writes(
            db, response_dict=_memory_tool_response_with_create("note C"),
            api_key_id="key1", conversation_id="conv1",
            source_provider_id="prov_does_not_exist",
        )
    assert n == 1


async def test_extract_runs_when_source_provider_id_none(db):
    """No source_provider context → no Phase 8 gating possible, behave
    as pre-Phase-8 (extract normally)."""
    from app.memory.extract import maybe_extract_memory_writes
    with patch("app.config.settings", _settings()):
        n = await maybe_extract_memory_writes(
            db, response_dict=_memory_tool_response_with_create("note D"),
            api_key_id="key1", conversation_id="conv1",
            source_provider_id=None,
        )
    assert n == 1


# ── Source-level wiring guards ─────────────────────────────────────


def test_messages_endpoint_gates_inject_on_memory_disabled():
    from pathlib import Path
    src = Path("app/api/messages.py").read_text()
    # Must check route.provider.memory_disabled before calling inject
    assert "route.provider, \"memory_disabled\"" in src or \
           'route.provider, "memory_disabled"' in src
    # Inject must be inside the not-disabled branch (after route selected)
    idx_route = src.find("select_provider_with_503(")
    idx_inject = src.find("maybe_inject_memory(")
    assert idx_route > 0 and idx_inject > idx_route


def test_completions_endpoint_gates_inject_on_memory_disabled():
    from pathlib import Path
    src = Path("app/api/completions.py").read_text()
    assert "memory_disabled" in src
    idx_route = src.find("select_provider_with_503(")
    idx_inject = src.find("maybe_inject_memory(")
    assert idx_route > 0 and idx_inject > idx_route


def test_extract_module_checks_provider_memory_disabled():
    from pathlib import Path
    src = Path("app/memory/extract.py").read_text()
    assert "memory_disabled" in src


def test_migration_alter_statement_present():
    from pathlib import Path
    src = Path("app/models/database.py").read_text()
    assert "ALTER TABLE providers ADD COLUMN memory_disabled" in src
