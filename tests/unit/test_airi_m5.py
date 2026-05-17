"""AIRI v4.0 milestone 5 — conversation history + cross-user search."""
from __future__ import annotations

import secrets

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.airi import history, tools
from app.models.db import (
    AiriConversation, AiriMessage, AiriProposal, AiriRule, AiriRuleset,
)


@pytest_asyncio.fixture
async def hist_env():
    """A clean slate for the AIRI conversation tables."""
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as c:
        await c.execute(delete(AiriMessage))
        await c.execute(delete(AiriConversation))
        await c.execute(delete(AiriProposal))
        await c.execute(delete(AiriRule))
        await c.execute(delete(AiriRuleset))
        await c.commit()
    yield AsyncSessionLocal


# ── title derivation ─────────────────────────────────────────────────────────

def test_title_from_collapses_and_truncates():
    assert history._title_from("  hello   world  ") == "hello world"
    assert history._title_from("") == "(untitled)"
    long = "x" * 200
    t = history._title_from(long)
    assert len(t) <= history._TITLE_MAX and t.endswith("…")


# ── start_turn / record_assistant ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_turn_creates_conversation(hist_env):
    SessionLocal = hist_env
    async with SessionLocal() as db:
        cid = await history.start_turn(
            db, user_id="alice", conversation_id=None,
            user_text="why is provider X slow?")
    assert cid
    async with SessionLocal() as db:
        conv = await db.get(AiriConversation, cid)
        msgs = (await db.execute(
            select(AiriMessage).where(AiriMessage.conversation_id == cid)
        )).scalars().all()
    assert conv.user_id == "alice"
    assert conv.title == "why is provider X slow?"
    assert len(msgs) == 1 and msgs[0].role == "user"


@pytest.mark.asyncio
async def test_start_turn_continues_existing(hist_env):
    SessionLocal = hist_env
    async with SessionLocal() as db:
        cid = await history.start_turn(
            db, user_id="alice", conversation_id=None, user_text="first")
    async with SessionLocal() as db:
        cid2 = await history.start_turn(
            db, user_id="alice", conversation_id=cid, user_text="second")
    assert cid2 == cid
    async with SessionLocal() as db:
        msgs = (await db.execute(
            select(AiriMessage).where(AiriMessage.conversation_id == cid)
        )).scalars().all()
    assert len(msgs) == 2


@pytest.mark.asyncio
async def test_start_turn_bad_id_mints_new(hist_env):
    """A conversation_id that does not resolve yields a fresh conversation —
    the chat never errors out because a stale id was sent."""
    SessionLocal = hist_env
    async with SessionLocal() as db:
        cid = await history.start_turn(
            db, user_id="alice", conversation_id="does-not-exist",
            user_text="hello")
    assert cid and cid != "does-not-exist"


@pytest.mark.asyncio
async def test_record_assistant_appends(hist_env):
    SessionLocal = hist_env
    async with SessionLocal() as db:
        cid = await history.start_turn(
            db, user_id="alice", conversation_id=None, user_text="q")
    async with SessionLocal() as db:
        await history.record_assistant(db, conversation_id=cid, text="the answer")
    async with SessionLocal() as db:
        detail = await history.get_conversation(db, cid)
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"] == "the answer"


@pytest.mark.asyncio
async def test_record_assistant_unknown_conversation_is_safe(hist_env):
    SessionLocal = hist_env
    async with SessionLocal() as db:
        await history.record_assistant(db, conversation_id="nope", text="x")
    # no exception — a missing conversation is a no-op


# ── list / get ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_conversations_is_per_user_recent_first(hist_env):
    SessionLocal = hist_env
    async with SessionLocal() as db:
        c_old = await history.start_turn(
            db, user_id="alice", conversation_id=None, user_text="old one")
    async with SessionLocal() as db:
        c_new = await history.start_turn(
            db, user_id="alice", conversation_id=None, user_text="new one")
    async with SessionLocal() as db:
        await history.start_turn(
            db, user_id="bob", conversation_id=None, user_text="bob's thread")
    async with SessionLocal() as db:
        mine = await history.list_conversations(db, user_id="alice")
    ids = [c["id"] for c in mine]
    assert ids == [c_new, c_old]                  # recent first
    assert all(c["user_id"] == "alice" for c in mine)  # not bob's
    assert mine[0]["message_count"] == 1


@pytest.mark.asyncio
async def test_get_conversation_missing_returns_none(hist_env):
    SessionLocal = hist_env
    async with SessionLocal() as db:
        assert await history.get_conversation(db, "missing") is None


# ── cross-user search ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_spans_all_users(hist_env):
    SessionLocal = hist_env
    async with SessionLocal() as db:
        a = await history.start_turn(
            db, user_id="alice", conversation_id=None,
            user_text="should we raise the Vertex provider priority?")
        await history.record_assistant(
            db, conversation_id=a, text="Vertex is healthy; raising priority is fine.")
    async with SessionLocal() as db:
        await history.start_turn(
            db, user_id="bob", conversation_id=None,
            user_text="bob asks about cohere latency")
    # alice can find bob's-and-her-own history; bob's search finds alice's
    async with SessionLocal() as db:
        hits = await history.search_messages(db, query="vertex priority")
    assert len(hits) >= 1
    assert any(h["user_id"] == "alice" for h in hits)
    assert all("vertex" in h["snippet"].lower() or h["role"] == "assistant"
               for h in hits)


@pytest.mark.asyncio
async def test_search_terms_are_anded(hist_env):
    SessionLocal = hist_env
    async with SessionLocal() as db:
        await history.start_turn(
            db, user_id="alice", conversation_id=None,
            user_text="the cohere provider had errors")
    async with SessionLocal() as db:
        # both terms present -> match
        assert await history.search_messages(db, query="cohere errors")
        # one term absent -> no match
        assert await history.search_messages(db, query="cohere unicorn") == []
        # empty query -> no match, no crash
        assert await history.search_messages(db, query="   ") == []


# ── the M5 read tools ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_conversations_tool(hist_env):
    SessionLocal = hist_env
    async with SessionLocal() as db:
        await history.start_turn(
            db, user_id="alice", conversation_id=None,
            user_text="disabled the openrouter provider for maintenance")
    out = await tools.run_tool("search_conversations", {"query": "openrouter"})
    assert out["match_count"] >= 1
    assert out["results"][0]["user_id"] == "alice"


@pytest.mark.asyncio
async def test_get_recent_changes_tool(hist_env):
    SessionLocal = hist_env
    async with SessionLocal() as db:
        db.add(AiriProposal(
            id=secrets.token_hex(8), kind="provider_change",
            target_id="p1", target_label="Vertex",
            change={"field": "priority", "from": 5, "to": 4},
            dry_run={}, status="applied", created_by="bob",
        ))
        await db.commit()
    out = await tools.run_tool("get_recent_changes", {})
    assert out["count"] >= 1
    rec = out["recent_changes"][0]
    assert rec["target"] == "Vertex" and rec["by"] == "bob"
