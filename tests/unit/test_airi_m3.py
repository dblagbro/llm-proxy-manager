"""AIRI v4.0 milestone 3 — propose / dry-run / apply / revert."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

import app.airi.agent as agent
from app.airi import dryrun, proposals, rules
from app.models.db import Provider


# ── dry-run engine (pure) ────────────────────────────────────────────────────

def test_dryrun_priority_change_reorders_ranking():
    provs = [
        {"name": "A", "priority": 5, "enabled": True},
        {"name": "B", "priority": 10, "enabled": True},
    ]
    impact = dryrun.provider_change_impact(
        field="priority", target_name="B", current_value=10, new_value=1,
        providers=provs, traffic_counts={"B": 50}, traffic_total=100,
    )
    assert impact["ranking_before"] == ["A", "B"]
    assert impact["ranking_after"] == ["B", "A"]
    assert impact["reordered"] is True
    assert impact["recent_traffic_share_pct"] == 50.0


def test_dryrun_disable_high_traffic_provider_warns():
    provs = [
        {"name": "A", "priority": 5, "enabled": True},
        {"name": "B", "priority": 10, "enabled": True},
    ]
    impact = dryrun.provider_change_impact(
        field="enabled", target_name="A", current_value=True, new_value=False,
        providers=provs, traffic_counts={"A": 40}, traffic_total=100,
    )
    assert impact["ranking_after"] == ["B"]
    assert any("recent traffic" in w for w in impact["warnings"])


def test_dryrun_disable_last_provider_warns():
    impact = dryrun.provider_change_impact(
        field="enabled", target_name="A", current_value=True, new_value=False,
        providers=[{"name": "A", "priority": 5, "enabled": True}],
        traffic_counts={}, traffic_total=0,
    )
    assert any("NO enabled providers" in w for w in impact["warnings"])


def test_dryrun_rule_change_direction():
    assert dryrun.rule_change_impact(
        rule_name="r", setting="max_priority_delta", current_value=2, new_value=4,
    )["direction"] == "widens"
    assert dryrun.rule_change_impact(
        rule_name="r", setting="max_priority_delta", current_value=4, new_value=1,
    )["direction"] == "tightens"


# ── proposal flow (DB) ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_ready():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, AiriRuleset, AiriRule, AiriProposal
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as c:
        await c.execute(delete(AiriProposal))
        await c.execute(delete(AiriRule))
        await c.execute(delete(AiriRuleset))
        await c.execute(delete(Provider).where(Provider.name.like("airi-m3-%")))
        await c.commit()
    async with AsyncSessionLocal() as c:
        c.add(Provider(name="airi-m3-alpha", provider_type="openai",
                       priority=5, enabled=True))
        c.add(Provider(name="airi-m3-beta", provider_type="openai",
                       priority=10, enabled=True))
        await c.commit()
    yield AsyncSessionLocal


async def _provider(SessionLocal, name) -> Provider:
    async with SessionLocal() as db:
        return (await db.execute(
            select(Provider).where(Provider.name == name)
        )).scalar_one()


@pytest.mark.asyncio
async def test_create_provider_change_caps_priority(db_ready):
    # alpha priority 5; ask for 0 (delta 5) — Default cap is 2 -> capped to 3.
    async with db_ready() as db:
        res = await proposals.create_provider_change(
            db, provider_ref="airi-m3-alpha", field="priority", value=0,
            mode="suggest", created_by="t", prompt="p",
        )
    assert res["status"] == "pending"
    assert res["change"]["to"] == 3
    assert res["change"]["capped"] is True


@pytest.mark.asyncio
async def test_apply_and_revert_provider_change(db_ready):
    async with db_ready() as db:
        res = await proposals.create_provider_change(
            db, provider_ref="airi-m3-alpha", field="priority", value=6,
            mode="suggest", created_by="t", prompt="p",
        )
    pid = res["proposal_id"]
    async with db_ready() as db:
        ap = await proposals.apply_proposal(db, pid, applied_by="t")
    assert ap["status"] == "applied"
    assert (await _provider(db_ready, "airi-m3-alpha")).priority == 6
    async with db_ready() as db:
        rv = await proposals.revert_proposal(db, pid, decided_by="t")
    assert rv["status"] == "reverted"
    assert (await _provider(db_ready, "airi-m3-alpha")).priority == 5  # restored


@pytest.mark.asyncio
async def test_mode_apply_applies_immediately(db_ready):
    async with db_ready() as db:
        res = await proposals.create_provider_change(
            db, provider_ref="airi-m3-beta", field="enabled", value=False,
            mode="apply", created_by="t", prompt="p",
        )
    assert res["status"] == "applied"
    assert (await _provider(db_ready, "airi-m3-beta")).enabled is False


@pytest.mark.asyncio
async def test_create_provider_change_rejects_bad_input(db_ready):
    async with db_ready() as db:
        assert "error" in await proposals.create_provider_change(
            db, provider_ref="airi-m3-alpha", field="bogus", value=1,
            mode="suggest", created_by="t", prompt="p")
        assert "error" in await proposals.create_provider_change(
            db, provider_ref="no-such-provider", field="priority", value=1,
            mode="suggest", created_by="t", prompt="p")
        # no-op change (alpha priority is already 5)
        assert "error" in await proposals.create_provider_change(
            db, provider_ref="airi-m3-alpha", field="priority", value=5,
            mode="suggest", created_by="t", prompt="p")


@pytest.mark.asyncio
async def test_apply_is_one_shot(db_ready):
    async with db_ready() as db:
        res = await proposals.create_provider_change(
            db, provider_ref="airi-m3-alpha", field="priority", value=6,
            mode="suggest", created_by="t", prompt="p")
        pid = res["proposal_id"]
        await proposals.apply_proposal(db, pid, applied_by="t")
        again = await proposals.apply_proposal(db, pid, applied_by="t")
    assert "error" in again  # already applied — cannot re-apply


@pytest.mark.asyncio
async def test_rule_change_proposal_apply_revert(db_ready):
    async with db_ready() as db:
        active = await rules.get_active_ruleset(db)
        rule = next(r for r in active["rules"]
                    if r["spec"]["setting"] == "max_priority_delta")
        rid = rule["id"]
        orig = rule["spec"]["value"]
        res = await proposals.create_rule_change(
            db, rule_id=rid, value=orig + 1, mode="apply",
            created_by="t", prompt="p")
    assert res["status"] == "applied"
    async with db_ready() as db:
        active = await rules.get_active_ruleset(db)
        rule = next(r for r in active["rules"] if r["id"] == rid)
    assert rule["spec"]["value"] == orig + 1
    async with db_ready() as db:
        await proposals.revert_proposal(db, res["proposal_id"], decided_by="t")
        active = await rules.get_active_ruleset(db)
        rule = next(r for r in active["rules"] if r["id"] == rid)
    assert rule["spec"]["value"] == orig  # restored


@pytest.mark.asyncio
async def test_list_proposals_is_audit_trail(db_ready):
    async with db_ready() as db:
        await proposals.create_provider_change(
            db, provider_ref="airi-m3-alpha", field="priority", value=6,
            mode="suggest", created_by="alice", prompt="raise alpha")
    async with db_ready() as db:
        rows = await proposals.list_proposals(db)
    assert len(rows) == 1
    assert rows[0]["created_by"] == "alice"
    assert rows[0]["created_via_prompt"] == "raise alpha"
    assert rows[0]["status"] == "pending"


# ── agent integration ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_emits_proposal_event(db_ready, monkeypatch):
    monkeypatch.setattr(agent.settings, "ai_provider_supervisor_internal_api_key", "k")
    calls = []

    async def fake_llm(api_key, model, messages):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": [{
                "type": "tool_use", "id": "t1", "name": "propose_provider_change",
                "input": {"provider": "airi-m3-alpha", "field": "priority",
                          "value": 6, "mode": "suggest"},
            }], "stop_reason": "tool_use"}
        return {"content": [{"type": "text", "text": "I proposed it."}],
                "stop_reason": "end_turn"}
    monkeypatch.setattr(agent, "_call_llm", fake_llm)

    events = [e async for e in agent.run_airi_turn(
        [{"role": "user", "content": "raise airi-m3-alpha priority"}], actor="tester")]
    kinds = [e[0] for e in events]
    assert "proposal" in kinds, "agent must emit a proposal event"
    prop = next(d for k, d in events if k == "proposal")
    assert prop["target"] == "airi-m3-alpha" and prop["status"] == "pending"
    assert events[-1][0] == "message"
