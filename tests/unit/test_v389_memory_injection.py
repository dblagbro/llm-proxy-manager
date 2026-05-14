"""v3.8.9 (#267) Phase 4 — memory injection middleware.

Covers ``app.memory.inject.maybe_inject_memory`` in isolation:
- No-op when caller_memory_enabled=False
- No-op when X-Conversation-Id missing
- No-op when no memory entry exists
- Anthropic shape: string system prompt → prefixed string
- Anthropic shape: list-of-blocks system → prefixed list
- Anthropic shape: no system → fresh system field
- OpenAI shape: leading system role → prefixed system content
- OpenAI shape: no leading system → synthesizes system at index 0
- Silent degrade on store exception
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.db import Base


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def enabled(monkeypatch):
    """Flip the feature on for the duration of one test."""
    from app.config import settings
    monkeypatch.setattr(settings, "caller_memory_enabled", True, raising=False)


# ── Gate: feature flag ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_op_when_disabled(db, monkeypatch):
    from app.memory.inject import maybe_inject_memory
    from app.memory.store import put
    from app.config import settings
    monkeypatch.setattr(settings, "caller_memory_enabled", False, raising=False)
    await put(db, api_key_id="k1", content="hello", conversation_id="c1")

    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
    out, injected = await maybe_inject_memory(
        db, body=body, api_key_id="k1",
        conversation_id="c1", memory_tag=None, endpoint="messages",
    )
    assert injected is False
    assert out == body


# ── Gate: conversation_id required ─────────────────────────────────


@pytest.mark.asyncio
async def test_no_op_without_conversation_id(db, enabled):
    from app.memory.inject import maybe_inject_memory
    from app.memory.store import put
    await put(db, api_key_id="k1", content="hello")

    body = {"model": "x"}
    out, injected = await maybe_inject_memory(
        db, body=body, api_key_id="k1",
        conversation_id=None, memory_tag=None, endpoint="messages",
    )
    assert injected is False
    assert out == body


# ── Gate: no entry → no-op ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_op_when_entry_missing(db, enabled):
    from app.memory.inject import maybe_inject_memory
    body = {"model": "x", "system": "be helpful"}
    out, injected = await maybe_inject_memory(
        db, body=body, api_key_id="k1",
        conversation_id="c-doesnt-exist", memory_tag=None,
        endpoint="messages",
    )
    assert injected is False
    assert out == body


# ── Anthropic shape ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_string_system_gets_prefix(db, enabled):
    from app.memory.inject import maybe_inject_memory, MEMORY_HEADER_PREFIX
    from app.memory.store import put
    await put(db, api_key_id="k1", content="user prefers brief replies",
              conversation_id="c1")

    body = {"model": "x", "system": "be polite"}
    out, injected = await maybe_inject_memory(
        db, body=body, api_key_id="k1",
        conversation_id="c1", memory_tag=None, endpoint="messages",
    )
    assert injected is True
    assert isinstance(out["system"], str)
    assert out["system"].startswith(MEMORY_HEADER_PREFIX)
    assert "user prefers brief replies" in out["system"]
    assert out["system"].endswith("be polite")


@pytest.mark.asyncio
async def test_anthropic_list_system_gets_prepended_block(db, enabled):
    from app.memory.inject import maybe_inject_memory
    from app.memory.store import put
    await put(db, api_key_id="k1", content="MEM", conversation_id="c1")

    body = {"model": "x", "system": [{"type": "text", "text": "ORIG"}]}
    out, injected = await maybe_inject_memory(
        db, body=body, api_key_id="k1",
        conversation_id="c1", memory_tag=None, endpoint="messages",
    )
    assert injected is True
    assert isinstance(out["system"], list)
    assert len(out["system"]) == 2
    assert out["system"][0]["type"] == "text"
    assert "MEM" in out["system"][0]["text"]
    assert out["system"][1] == {"type": "text", "text": "ORIG"}


@pytest.mark.asyncio
async def test_anthropic_no_system_creates_one(db, enabled):
    from app.memory.inject import maybe_inject_memory
    from app.memory.store import put
    await put(db, api_key_id="k1", content="MEM", conversation_id="c1")

    body = {"model": "x"}
    out, injected = await maybe_inject_memory(
        db, body=body, api_key_id="k1",
        conversation_id="c1", memory_tag=None, endpoint="messages",
    )
    assert injected is True
    assert "MEM" in out["system"]


# ── OpenAI shape ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_existing_system_gets_prefix(db, enabled):
    from app.memory.inject import maybe_inject_memory
    from app.memory.store import put
    await put(db, api_key_id="k1", content="MEM", conversation_id="c1")

    body = {
        "model": "x",
        "messages": [
            {"role": "system", "content": "be polite"},
            {"role": "user", "content": "hi"},
        ],
    }
    out, injected = await maybe_inject_memory(
        db, body=body, api_key_id="k1",
        conversation_id="c1", memory_tag=None, endpoint="completions",
    )
    assert injected is True
    assert out["messages"][0]["role"] == "system"
    assert "MEM" in out["messages"][0]["content"]
    assert "be polite" in out["messages"][0]["content"]
    # User message untouched
    assert out["messages"][1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_openai_no_system_synthesizes_one(db, enabled):
    from app.memory.inject import maybe_inject_memory
    from app.memory.store import put
    await put(db, api_key_id="k1", content="MEM", conversation_id="c1")

    body = {
        "model": "x",
        "messages": [{"role": "user", "content": "hi"}],
    }
    out, injected = await maybe_inject_memory(
        db, body=body, api_key_id="k1",
        conversation_id="c1", memory_tag=None, endpoint="completions",
    )
    assert injected is True
    assert out["messages"][0]["role"] == "system"
    assert "MEM" in out["messages"][0]["content"]
    assert out["messages"][1] == {"role": "user", "content": "hi"}


# ── Silent degrade ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_silent_degrade_on_store_error(db, enabled, monkeypatch):
    """If the store raises, the request body is forwarded unchanged."""
    from app.memory import inject as inject_mod

    async def boom(*a, **kw):
        raise RuntimeError("redis down + sqlite locked")

    monkeypatch.setattr(inject_mod, "get", boom, raising=False)
    # Patch the resolved binding inside maybe_inject_memory by replacing
    # the get function in app.memory.store as well (import is local).
    import app.memory.store as store_mod
    monkeypatch.setattr(store_mod, "get", boom)

    body = {"model": "x", "system": "be polite"}
    out, injected = await inject_mod.maybe_inject_memory(
        db, body=body, api_key_id="k1",
        conversation_id="c1", memory_tag=None, endpoint="messages",
    )
    assert injected is False
    assert out == body


# ── Source-level guards ───────────────────────────────────────────


def test_messages_endpoint_wires_memory_header():
    from pathlib import Path
    src = Path("app/api/messages.py").read_text()
    assert "x_conversation_id" in src
    assert "x-conversation-id" in src
    assert "maybe_inject_memory" in src
    assert "X-Caller-Memory" in src


def test_completions_endpoint_wires_memory_header():
    from pathlib import Path
    src = Path("app/api/completions.py").read_text()
    assert "x_conversation_id" in src
    assert "x-conversation-id" in src
    assert "maybe_inject_memory" in src
    assert "X-Caller-Memory" in src


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 8, 9)
