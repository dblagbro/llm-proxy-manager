"""v3.9.0 (#267) Phase 5 — Anthropic memory-tool write-back.

Covers ``app.memory.extract.maybe_extract_memory_writes`` in isolation:
- No-op when caller_memory_enabled=False
- No-op when conversation_id missing
- No-op when response has no memory tool_use blocks
- ``create`` persists content + path → memory_tag
- ``str_replace`` does read-modify-write
- ``insert`` adds a line at the requested position
- ``delete`` tombstones the row
- ``rename`` does put(new) + delete(old)
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
    from app.config import settings
    monkeypatch.setattr(settings, "caller_memory_enabled", True, raising=False)


def _memory_tool_use(command, **input_fields):
    """Build a synthetic Anthropic response containing one memory
    tool_use block with the given command + input fields."""
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_abc",
                "name": "memory_20250818",
                "input": {"command": command, **input_fields},
            }
        ],
        "model": "claude-3-5-sonnet",
        "stop_reason": "tool_use",
    }


# ── Gates ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_op_when_disabled(db, monkeypatch):
    from app.memory.extract import maybe_extract_memory_writes
    from app.config import settings
    monkeypatch.setattr(settings, "caller_memory_enabled", False, raising=False)
    resp = _memory_tool_use("create", path="/memories/x.md", content="hi")
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id="c1",
    )
    assert n == 0


@pytest.mark.asyncio
async def test_no_op_without_conversation_id(db, enabled):
    from app.memory.extract import maybe_extract_memory_writes
    resp = _memory_tool_use("create", path="/memories/x.md", content="hi")
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id=None,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_no_op_when_no_memory_tool_use(db, enabled):
    from app.memory.extract import maybe_extract_memory_writes
    resp = {
        "content": [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {}},
        ]
    }
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id="c1",
    )
    assert n == 0


# ── create ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_persists_content(db, enabled):
    from app.memory.extract import maybe_extract_memory_writes
    from app.memory.store import get
    resp = _memory_tool_use(
        "create", path="/memories/user_facts.md", content="name=Alice"
    )
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id="c1",
        source_provider_id="prov-A",
    )
    assert n == 1
    out = await get(db, "k1", "c1", "user_facts.md")
    assert out is not None
    assert out.content == "name=Alice"
    assert out.source_provider_id == "prov-A"


# ── str_replace ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_str_replace_read_modify_write(db, enabled):
    from app.memory.extract import maybe_extract_memory_writes
    from app.memory.store import put, get
    await put(db, api_key_id="k1", content="user lives in Seattle",
              conversation_id="c1", memory_tag="user_facts.md")
    resp = _memory_tool_use(
        "str_replace",
        path="/memories/user_facts.md",
        old_str="Seattle", new_str="Portland",
    )
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id="c1",
    )
    assert n == 1
    out = await get(db, "k1", "c1", "user_facts.md")
    assert out.content == "user lives in Portland"


@pytest.mark.asyncio
async def test_str_replace_no_op_when_old_str_missing(db, enabled):
    from app.memory.extract import maybe_extract_memory_writes
    from app.memory.store import put
    await put(db, api_key_id="k1", content="some text",
              conversation_id="c1", memory_tag="x.md")
    resp = _memory_tool_use(
        "str_replace", path="/memories/x.md",
        old_str="not present", new_str="whatever",
    )
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id="c1",
    )
    assert n == 0


# ── insert ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insert_at_line(db, enabled):
    from app.memory.extract import maybe_extract_memory_writes
    from app.memory.store import put, get
    await put(db, api_key_id="k1", content="line1\nline3",
              conversation_id="c1", memory_tag="x.md")
    resp = _memory_tool_use(
        "insert", path="/memories/x.md",
        insert_line=2, content="line2",
    )
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id="c1",
    )
    assert n == 1
    out = await get(db, "k1", "c1", "x.md")
    assert out.content == "line1\nline2\nline3"


# ── delete ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_tombstones(db, enabled):
    from app.memory.extract import maybe_extract_memory_writes
    from app.memory.store import put, get
    await put(db, api_key_id="k1", content="goodbye",
              conversation_id="c1", memory_tag="x.md")
    resp = _memory_tool_use("delete", path="/memories/x.md")
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id="c1",
    )
    assert n == 1
    assert await get(db, "k1", "c1", "x.md") is None


# ── rename ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_puts_new_and_deletes_old(db, enabled):
    from app.memory.extract import maybe_extract_memory_writes
    from app.memory.store import put, get
    await put(db, api_key_id="k1", content="alice",
              conversation_id="c1", memory_tag="old.md")
    resp = _memory_tool_use(
        "rename", path="/memories/old.md", new_path="/memories/new.md",
    )
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id="c1",
    )
    assert n == 1
    assert (await get(db, "k1", "c1", "new.md")).content == "alice"
    assert await get(db, "k1", "c1", "old.md") is None


# ── view is read-only ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_view_is_noop(db, enabled):
    from app.memory.extract import maybe_extract_memory_writes
    resp = _memory_tool_use("view", path="/memories/x.md")
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id="c1",
    )
    assert n == 0


# ── Multi-block batches ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_writes_in_one_response(db, enabled):
    from app.memory.extract import maybe_extract_memory_writes
    from app.memory.store import get
    resp = {
        "content": [
            {"type": "text", "text": "Let me update memory."},
            {
                "type": "tool_use", "id": "t1", "name": "memory_20250818",
                "input": {"command": "create", "path": "/memories/a.md", "content": "A"},
            },
            {
                "type": "tool_use", "id": "t2", "name": "memory_20250818",
                "input": {"command": "create", "path": "/memories/b.md", "content": "B"},
            },
        ]
    }
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id="c1",
    )
    assert n == 2
    assert (await get(db, "k1", "c1", "a.md")).content == "A"
    assert (await get(db, "k1", "c1", "b.md")).content == "B"


# ── Silent degrade ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_silent_degrade_on_store_error(db, enabled, monkeypatch):
    """Store exceptions must not break response forwarding."""
    import app.memory.store as store_mod

    async def boom(*a, **kw):
        raise RuntimeError("store down")

    monkeypatch.setattr(store_mod, "put", boom)

    from app.memory.extract import maybe_extract_memory_writes
    resp = _memory_tool_use("create", path="/memories/x.md", content="hi")
    n = await maybe_extract_memory_writes(
        db, response_dict=resp, api_key_id="k1", conversation_id="c1",
    )
    assert n == 0  # Silent degrade — store error must NOT propagate


# ── Source-level guards ───────────────────────────────────────────


def test_messages_endpoint_wires_write_back():
    from pathlib import Path
    src = Path("app/api/messages.py").read_text()
    assert "maybe_extract_memory_writes" in src
    assert "X-Caller-Memory-Writes" in src


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 9, 0)
