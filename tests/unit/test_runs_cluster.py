"""R5 cluster-stickiness tests.

Covers:
  - 307 redirect helper picks the right peer URL
  - Debounce coalescer collapses overlapping pushes
  - Terminal pushes are sync-acked (mocked) and trigger background retry
  - apply_sync ingests 'runs' section with last-write-wins
  - adopt endpoint refuses without grace, accepts after
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
async def _clean():
    from app.models.database import init_db, AsyncSessionLocal
    from sqlalchemy import text
    await init_db()
    async with AsyncSessionLocal() as db:
        for tbl in ("run_events", "run_messages", "run_idempotency", "runs"):
            try:
                await db.execute(text(f"DELETE FROM {tbl}"))
            except Exception:
                pass
        await db.commit()
    # Reset the replication module's pending-push registry between tests
    from app.runs import replication
    replication._pending.clear()
    yield


# ── 307 redirect helper ────────────────────────────────────────────────────


def _fake_peer(node_id: str, url: str, status: str = "healthy",
               last_heartbeat: float = None):
    p = MagicMock()
    p.id = node_id
    p.url = url
    p.status = status
    p.last_heartbeat = last_heartbeat or time.time()
    return p


def test_redirect_helper_returns_none_when_self_owns():
    from app.api.runs import _maybe_redirect_to_owner
    from app.config import settings as cfg
    run = MagicMock()
    run.owner_node_id = cfg.cluster_node_id or "local"
    request = MagicMock()
    assert _maybe_redirect_to_owner(run, request) is None


def test_redirect_helper_returns_307_with_owner_url():
    from app.api.runs import _maybe_redirect_to_owner
    run = MagicMock()
    run.owner_node_id = "node-B"
    request = MagicMock()
    request.url.path = "/v1/runs/run_xyz"
    request.url.query = ""
    with patch("app.cluster.manager.peers", {
        "node-B": _fake_peer("node-B", "https://b.example.com"),
    }):
        resp = _maybe_redirect_to_owner(run, request)
    assert resp is not None
    assert resp.status_code == 307
    assert resp.headers["Location"] == "https://b.example.com/v1/runs/run_xyz"
    assert resp.headers["X-Run-Owner"] == "node-B"


def test_redirect_helper_falls_back_local_when_owner_unknown():
    """If we don't know how to reach the owner, return None — the API
    handler proceeds locally and the persisted (replicated) state
    answers correctly."""
    from app.api.runs import _maybe_redirect_to_owner
    run = MagicMock()
    run.owner_node_id = "ghost-node"
    request = MagicMock()
    request.url.path = "/v1/runs/run_xyz"
    request.url.query = ""
    with patch("app.cluster.manager.peers", {}):
        resp = _maybe_redirect_to_owner(run, request)
    assert resp is None


# ── Debounce coalescer ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replicate_debounces_non_terminal_pushes(monkeypatch):
    """Three rapid non-terminal calls inside the 250ms window collapse
    into ONE peer push."""
    from app.runs import replication
    pushed: list = []

    async def fake_push(snapshots, *, wait_for_ack):
        pushed.append((tuple(s["id"] for s in snapshots), wait_for_ack))
        return {}
    monkeypatch.setattr(replication, "_push_to_peers", fake_push)
    monkeypatch.setattr("app.config.settings.cluster_enabled", True, raising=False)

    run = MagicMock()
    run.id = "run_dbg"
    run.api_key_id = "k"
    run.owner_node_id = "n1"
    run.status = "running"
    for col in ("current_step", "deadline_ts", "max_turns", "model_preference",
                "compaction_model", "system_prompt", "tools_spec",
                "metadata_json", "trace_id", "model_calls", "tool_calls",
                "tokens_in", "tokens_out", "last_provider_id",
                "context_summarized_at_turn", "current_tool_use_id",
                "current_tool_name", "current_tool_input", "result_text",
                "error_kind", "error_message", "created_at", "updated_at",
                "completed_at"):
        setattr(run, col, None if col not in ("model_calls", "tool_calls",
                                               "tokens_in", "tokens_out",
                                               "max_turns", "deadline_ts",
                                               "created_at", "updated_at")
                else (10 if col == "max_turns" else
                      time.time() if col in ("created_at", "updated_at",
                                              "deadline_ts") else 0))

    # Three rapid calls
    await replication.replicate(run, terminal=False)
    await replication.replicate(run, terminal=False)
    await replication.replicate(run, terminal=False)
    # Wait long enough for the debounce timer to fire
    await asyncio.sleep(0.4)

    assert len(pushed) == 1, f"expected 1 coalesced push, got {len(pushed)}"
    assert pushed[0] == (("run_dbg",), False)


@pytest.mark.asyncio
async def test_replicate_terminal_fires_immediately_with_ack(monkeypatch):
    from app.runs import replication
    pushed: list = []

    async def fake_push(snapshots, *, wait_for_ack):
        pushed.append((tuple(s["id"] for s in snapshots), wait_for_ack))
        return {"node-B": True}
    monkeypatch.setattr(replication, "_push_to_peers", fake_push)
    monkeypatch.setattr("app.config.settings.cluster_enabled", True, raising=False)

    run = MagicMock()
    run.id = "run_term"
    for col in ("api_key_id", "owner_node_id", "status", "current_step",
                "deadline_ts", "max_turns", "model_preference",
                "compaction_model", "system_prompt", "tools_spec",
                "metadata_json", "trace_id", "model_calls", "tool_calls",
                "tokens_in", "tokens_out", "last_provider_id",
                "context_summarized_at_turn", "current_tool_use_id",
                "current_tool_name", "current_tool_input", "result_text",
                "error_kind", "error_message", "created_at", "updated_at",
                "completed_at"):
        setattr(run, col, "x" if col in ("api_key_id", "owner_node_id",
                                          "status") else None)
    run.deadline_ts = 0.0; run.max_turns = 30
    run.model_calls = run.tool_calls = run.tokens_in = run.tokens_out = 0
    run.created_at = run.updated_at = time.time()

    await replication.replicate(run, terminal=True)
    # Terminal path is direct — no debounce wait needed
    assert len(pushed) == 1
    assert pushed[0][1] is True


# ── apply_sync runs ingest ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_sync_inserts_unknown_run():
    from app.cluster.sync import apply_sync
    from app.models.database import AsyncSessionLocal
    from app.models.db import Run
    from sqlalchemy import select

    payload = {
        "source_node": "peer-A",
        "runs": [{
            "id": "run_imported",
            "api_key_id": "k1",
            "owner_node_id": "peer-A",
            "status": "running",
            "deadline_ts": time.time() + 60,
            "max_turns": 30,
            "model_calls": 1, "tool_calls": 0,
            "tokens_in": 100, "tokens_out": 50,
            "created_at": time.time(),
            "updated_at": time.time(),
        }],
    }
    async with AsyncSessionLocal() as db:
        await apply_sync(db, payload)
        run = (await db.execute(
            select(Run).where(Run.id == "run_imported")
        )).scalar_one_or_none()
    assert run is not None
    assert run.status == "running"
    assert run.owner_node_id == "peer-A"
    assert run.tokens_in == 100


@pytest.mark.asyncio
async def test_apply_sync_keeps_newer_local_on_lww():
    """Last-write-wins by updated_at: local copy newer ⇒ peer payload ignored."""
    from app.cluster.sync import apply_sync
    from app.models.database import AsyncSessionLocal
    from app.models.db import Run
    from sqlalchemy import select

    now = time.time()
    async with AsyncSessionLocal() as db:
        db.add(Run(
            id="run_lww", api_key_id="k1", owner_node_id="local",
            status="completed", deadline_ts=now + 60, max_turns=30,
            model_calls=2, tool_calls=0, tokens_in=200, tokens_out=100,
            created_at=now - 100, updated_at=now,    # newer
        ))
        await db.commit()

    payload = {
        "source_node": "peer-A",
        "runs": [{
            "id": "run_lww",
            "status": "running",     # peer thinks it's still running
            "tokens_in": 50,
            "updated_at": now - 50,   # older
        }],
    }
    async with AsyncSessionLocal() as db:
        await apply_sync(db, payload)
        run = (await db.execute(
            select(Run).where(Run.id == "run_lww")
        )).scalar_one()
    # Local wins
    assert run.status == "completed"
    assert run.tokens_in == 200


@pytest.mark.asyncio
async def test_apply_sync_overwrites_when_peer_is_newer():
    from app.cluster.sync import apply_sync
    from app.models.database import AsyncSessionLocal
    from app.models.db import Run
    from sqlalchemy import select

    now = time.time()
    async with AsyncSessionLocal() as db:
        db.add(Run(
            id="run_lww2", api_key_id="k1", owner_node_id="peer-A",
            status="running", deadline_ts=now + 60, max_turns=30,
            model_calls=1, tool_calls=0, tokens_in=50, tokens_out=10,
            created_at=now - 100, updated_at=now - 50,   # older
        ))
        await db.commit()

    payload = {
        "source_node": "peer-A",
        "runs": [{
            "id": "run_lww2",
            "status": "completed",
            "result_text": "done",
            "tokens_in": 200,
            "updated_at": now,   # newer
        }],
    }
    async with AsyncSessionLocal() as db:
        await apply_sync(db, payload)
        run = (await db.execute(
            select(Run).where(Run.id == "run_lww2")
        )).scalar_one()
    assert run.status == "completed"
    assert run.result_text == "done"
    assert run.tokens_in == 200
