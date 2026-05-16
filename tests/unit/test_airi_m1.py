"""AIRI v4.0 milestone 1 — read-only chat. Tests for the tools + agent loop."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete

import app.airi.agent as agent
from app.airi.tools import run_tool, _clamp_int, READ_ONLY_TOOLS


# ── tools ────────────────────────────────────────────────────────────────────

def test_clamp_int():
    assert _clamp_int("5", 20, 1, 100) == 5
    assert _clamp_int(None, 20, 1, 100) == 20
    assert _clamp_int("nonsense", 20, 1, 100) == 20
    assert _clamp_int(999, 20, 1, 100) == 100
    assert _clamp_int(-3, 20, 1, 100) == 1


@pytest.mark.asyncio
async def test_run_tool_unknown_returns_error():
    out = await run_tool("no_such_tool", {})
    assert "error" in out


@pytest.mark.asyncio
async def test_get_supervisor_state_shape():
    out = await run_tool("get_supervisor_state", {})
    assert "enabled" in out and "mode" in out and "caps" in out
    assert out["mode"] in ("suggest-only", "auto-apply")


@pytest.mark.asyncio
async def test_explain_routing_has_steps():
    out = await run_tool("explain_routing", {})
    assert isinstance(out.get("steps"), list) and len(out["steps"]) >= 5


@pytest.mark.asyncio
async def test_get_routing_config_shape():
    out = await run_tool("get_routing_config", {})
    assert "fallback_enabled" in out and "note" in out


@pytest_asyncio.fixture
async def db_ready():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, Provider
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(Provider).where(Provider.name.like("airi-m1-%")))
        await cleanup.commit()
    yield AsyncSessionLocal


@pytest.mark.asyncio
async def test_get_provider_health_lists_providers(db_ready):
    from app.models.db import Provider
    async with db_ready() as db:
        db.add(Provider(name="airi-m1-prov", provider_type="openai",
                         priority=5, enabled=True))
        await db.commit()
    out = await run_tool("get_provider_health", {})
    assert out["provider_count"] >= 1
    assert any(p["name"] == "airi-m1-prov" for p in out["providers"])


# ── agent loop ───────────────────────────────────────────────────────────────

async def _collect(messages):
    return [ev async for ev in agent.run_airi_turn(messages)]


@pytest.mark.asyncio
async def test_agent_errors_without_internal_key(monkeypatch):
    monkeypatch.setattr(agent.settings, "ai_provider_supervisor_internal_api_key", "")
    events = await _collect([{"role": "user", "content": "hi"}])
    assert events and events[0][0] == "error"


@pytest.mark.asyncio
async def test_agent_returns_final_answer(monkeypatch):
    monkeypatch.setattr(agent.settings, "ai_provider_supervisor_internal_api_key", "k")

    async def fake_llm(api_key, model, messages):
        return {"content": [{"type": "text", "text": "Routing looks healthy."}],
                "stop_reason": "end_turn"}
    monkeypatch.setattr(agent, "_call_llm", fake_llm)

    events = await _collect([{"role": "user", "content": "status?"}])
    assert ("message", {"text": "Routing looks healthy."}) in events


@pytest.mark.asyncio
async def test_agent_runs_a_tool_then_answers(monkeypatch):
    monkeypatch.setattr(agent.settings, "ai_provider_supervisor_internal_api_key", "k")
    calls = []

    async def fake_llm(api_key, model, messages):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": [{"type": "tool_use", "id": "t1",
                                 "name": "get_supervisor_state", "input": {}}],
                    "stop_reason": "tool_use"}
        # second call sees the tool_result appended
        return {"content": [{"type": "text", "text": "The supervisor is configured."}],
                "stop_reason": "end_turn"}
    monkeypatch.setattr(agent, "_call_llm", fake_llm)

    events = await _collect([{"role": "user", "content": "is the supervisor on?"}])
    assert any(e[0] == "status" for e in events), "expected a status event while the tool ran"
    assert events[-1] == ("message", {"text": "The supervisor is configured."})
    # the second LLM call must have received a tool_result
    assert any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
        for m in calls[1]
    )


@pytest.mark.asyncio
async def test_agent_rejects_non_readonly_tool(monkeypatch):
    """Milestone 1 exposes only read tools — if the model asks for anything
    else the loop must refuse it, not execute it."""
    monkeypatch.setattr(agent.settings, "ai_provider_supervisor_internal_api_key", "k")
    assert "set_provider_priority" not in READ_ONLY_TOOLS
    calls = []

    async def fake_llm(api_key, model, messages):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": [{"type": "tool_use", "id": "t1",
                                 "name": "set_provider_priority",
                                 "input": {"provider_id": "x", "priority": 1}}],
                    "stop_reason": "tool_use"}
        return {"content": [{"type": "text", "text": "I cannot change anything yet."}],
                "stop_reason": "end_turn"}
    monkeypatch.setattr(agent, "_call_llm", fake_llm)

    await _collect([{"role": "user", "content": "raise provider x priority"}])
    # the tool_result fed back must be a refusal, not an execution
    tool_results = [
        b for m in calls[1] if isinstance(m.get("content"), list)
        for b in m["content"] if b.get("type") == "tool_result"
    ]
    assert tool_results and "not available" in tool_results[0]["content"]


@pytest.mark.asyncio
async def test_agent_surfaces_llm_failure(monkeypatch):
    monkeypatch.setattr(agent.settings, "ai_provider_supervisor_internal_api_key", "k")

    async def boom(api_key, model, messages):
        raise RuntimeError("upstream down")
    monkeypatch.setattr(agent, "_call_llm", boom)

    events = await _collect([{"role": "user", "content": "hi"}])
    assert events and events[-1][0] == "error"
