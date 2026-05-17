"""AIRI v4.0 milestone 4 — scheduled rules, monitors, the evaluator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.airi import evaluator, proposals, rules
from app.models.db import Provider, ActivityLog, AiriRule


def _naive_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def eval_env():
    """A provider with a known error rate (4 of 10 recent requests failed)."""
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, AiriRuleset, AiriRule, AiriProposal
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as c:
        await c.execute(delete(AiriProposal))
        await c.execute(delete(AiriRule))
        await c.execute(delete(AiriRuleset))
        await c.execute(delete(Provider).where(Provider.name.like("airi-m4-%")))
        await c.commit()
    async with AsyncSessionLocal() as c:
        p = Provider(name="airi-m4-prov", provider_type="openai", priority=5, enabled=True)
        c.add(p)
        await c.commit()
        await c.refresh(p)
        pid = p.id
        for i in range(10):
            c.add(ActivityLog(
                event_type="llm_request", provider_id=pid,
                severity="error" if i < 4 else "info", event_meta={},
            ))
        await c.commit()
    yield AsyncSessionLocal, pid


# ── condition evaluation ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provider_error_rate(eval_env):
    SessionLocal, pid = eval_env
    async with SessionLocal() as db:
        total, errors, pct = await evaluator._provider_error_rate(db, pid, 60)
    assert total == 10 and errors == 4 and pct == 40.0


@pytest.mark.asyncio
async def test_evaluate_condition_fires_over_threshold(eval_env):
    SessionLocal, pid = eval_env
    cond = {"metric": "error_rate_pct", "provider_id": pid,
            "window_min": 60, "op": ">", "value": 20}
    async with SessionLocal() as db:
        fired, observed, _ = await evaluator.evaluate_condition(db, cond)
    assert fired is True and observed == 40.0


@pytest.mark.asyncio
async def test_evaluate_condition_below_threshold(eval_env):
    SessionLocal, pid = eval_env
    cond = {"metric": "error_rate_pct", "provider_id": pid,
            "window_min": 60, "op": ">", "value": 80}
    async with SessionLocal() as db:
        fired, _, _ = await evaluator.evaluate_condition(db, cond)
    assert fired is False


# ── add-rule proposal ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_rule_proposal_apply_revert(eval_env):
    SessionLocal, pid = eval_env
    async with SessionLocal() as db:
        res = await proposals.create_add_rule(
            db, rule_type="conditional", name="m4-test-rule",
            provider_ref="airi-m4-prov", window_min=30, op=">", threshold=25,
            cadence_min=5, action_hours=2, action_mode="suggest",
            created_by="t", prompt="watch the provider")
    assert res["status"] == "pending" and res["kind"] == "add_rule"
    pid_prop = res["proposal_id"]
    async with SessionLocal() as db:
        ap = await proposals.apply_proposal(db, pid_prop, applied_by="t")
    assert ap["status"] == "applied"
    async with SessionLocal() as db:
        active = await rules.get_active_ruleset(db)
        conds = [r for r in active["rules"] if r["kind"] == "conditional"]
    assert len(conds) == 1 and conds[0]["name"] == "m4-test-rule"
    # revert removes the rule
    async with SessionLocal() as db:
        await proposals.revert_proposal(db, pid_prop, decided_by="t")
        active = await rules.get_active_ruleset(db)
    assert not [r for r in active["rules"] if r["kind"] == "conditional"]


@pytest.mark.asyncio
async def test_add_rule_rejects_bad_input(eval_env):
    SessionLocal, _ = eval_env
    async with SessionLocal() as db:
        assert "error" in await proposals.create_add_rule(
            db, rule_type="bogus", name="x", provider_ref="airi-m4-prov",
            window_min=10, op=">", threshold=5, cadence_min=5, action_hours=1,
            action_mode="suggest", created_by="t", prompt="p")
        assert "error" in await proposals.create_add_rule(
            db, rule_type="conditional", name="x", provider_ref="no-such",
            window_min=10, op=">", threshold=5, cadence_min=5, action_hours=1,
            action_mode="suggest", created_by="t", prompt="p")


# ── the evaluator ────────────────────────────────────────────────────────────

async def _add_conditional(SessionLocal, *, action_mode="suggest", cadence=1):
    async with SessionLocal() as db:
        res = await proposals.create_add_rule(
            db, rule_type="conditional", name="m4-eval-rule",
            provider_ref="airi-m4-prov", window_min=60, op=">", threshold=20,
            cadence_min=cadence, action_hours=1, action_mode=action_mode,
            created_by="t", prompt="auto-skip on errors")
        await proposals.apply_proposal(db, res["proposal_id"], applied_by="t")
    async with SessionLocal() as db:
        active = await rules.get_active_ruleset(db)
        return next(r["id"] for r in active["rules"] if r["kind"] == "conditional")


@pytest.mark.asyncio
async def test_evaluate_due_rules_conditional_fires(eval_env):
    SessionLocal, pid = eval_env
    await _add_conditional(SessionLocal)
    async with SessionLocal() as db:
        summary, notes = await evaluator.evaluate_due_rules(db)
    assert summary["fired"] >= 1 and summary["acted"] >= 1
    assert len(notes) >= 1
    # the rule's action created a pending provider-change proposal
    async with SessionLocal() as db:
        props = await proposals.list_proposals(db, status="pending")
    assert any(p["kind"] == "provider_change" for p in props)


@pytest.mark.asyncio
async def test_evaluate_monitor_notifies(eval_env):
    SessionLocal, pid = eval_env
    async with SessionLocal() as db:
        res = await proposals.create_add_rule(
            db, rule_type="monitor", name="m4-monitor",
            provider_ref="airi-m4-prov", window_min=60, op=">", threshold=20,
            cadence_min=1, action_hours=None, action_mode="suggest",
            created_by="t", prompt="watch")
        await proposals.apply_proposal(db, res["proposal_id"], applied_by="t")
    async with SessionLocal() as db:
        summary, notes = await evaluator.evaluate_due_rules(db)
    assert summary["notified"] >= 1
    assert any("monitor" in n[0] for n in notes)


@pytest.mark.asyncio
async def test_oscillation_breaker_trips(eval_env):
    SessionLocal, pid = eval_env
    rule_id = await _add_conditional(SessionLocal)
    # pre-load the rule as having already acted at its cap — the prior act
    # is 30 min ago, past the cooldown window, so the breaker (not the
    # cooldown) is what trips.
    async with SessionLocal() as db:
        r = await db.get(AiriRule, rule_id)
        r.max_runs_per_window = 1
        r.oscillation_state = {
            "acts": [(_naive_now() - timedelta(minutes=30)).isoformat()],
        }
        await db.commit()
    async with SessionLocal() as db:
        r = await db.get(AiriRule, rule_id)
        outcome, note = await evaluator._act_conditional(db, r, r.spec, "detail")
        await db.commit()
    assert outcome == "tripped"
    async with SessionLocal() as db:
        r = await db.get(AiriRule, rule_id)
    assert r.enabled is False, "a tripped rule must be disabled"


@pytest.mark.asyncio
async def test_cooldown_holds_off_a_second_action(eval_env):
    SessionLocal, pid = eval_env
    rule_id = await _add_conditional(SessionLocal)
    async with SessionLocal() as db:
        r = await db.get(AiriRule, rule_id)
        r.cooldown_sec = 3600
        r.oscillation_state = {"acts": [_naive_now().isoformat()]}  # just acted
        await db.commit()
    async with SessionLocal() as db:
        r = await db.get(AiriRule, rule_id)
        outcome, _ = await evaluator._act_conditional(db, r, r.spec, "detail")
    assert outcome == "cooled_down"


# ── kill switch + rule toggle ────────────────────────────────────────────────

def test_automation_kill_switch():
    evaluator.set_automation(True)
    assert evaluator.is_automation_enabled() is True
    evaluator.set_automation(False)
    assert evaluator.is_automation_enabled() is False


@pytest.mark.asyncio
async def test_toggle_rule(eval_env):
    SessionLocal, _ = eval_env
    rule_id = await _add_conditional(SessionLocal)
    async with SessionLocal() as db:
        out = await rules.toggle_rule(db, rule_id)
    assert out["enabled"] is False
    async with SessionLocal() as db:
        out = await rules.toggle_rule(db, rule_id)
    assert out["enabled"] is True
