"""AIRI v4.0 milestone 2 — rules layer + rule-sets."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.airi import rules
from app.airi.tools import run_tool


@pytest_asyncio.fixture
async def db_ready():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, AiriRuleset, AiriRule
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as c:
        await c.execute(delete(AiriRule))
        await c.execute(delete(AiriRuleset))
        await c.commit()
    yield AsyncSessionLocal


@pytest.mark.asyncio
async def test_ensure_seeded_creates_default(db_ready):
    async with db_ready() as db:
        await rules.ensure_seeded(db)
    async with db_ready() as db:
        sets = await rules.list_rulesets(db)
    assert len(sets) == 1
    assert sets[0]["name"] == "Default"
    assert sets[0]["is_default"] and sets[0]["is_active"]


@pytest.mark.asyncio
async def test_default_has_threshold_rules(db_ready):
    async with db_ready() as db:
        active = await rules.get_active_ruleset(db)
    assert active["name"] == "Default"
    assert len(active["rules"]) == 4
    assert all(r["kind"] == "threshold" for r in active["rules"])
    assert "max_priority_delta" in {r["spec"]["setting"] for r in active["rules"]}


@pytest.mark.asyncio
async def test_seed_is_idempotent(db_ready):
    async with db_ready() as db:
        await rules.ensure_seeded(db)
    async with db_ready() as db:
        await rules.ensure_seeded(db)
    async with db_ready() as db:
        assert len(await rules.list_rulesets(db)) == 1


@pytest.mark.asyncio
async def test_save_as_snapshots_active_without_activating(db_ready):
    async with db_ready() as db:
        res = await rules.save_as(db, "Aggressive", created_by="tester")
    assert res.get("ok")
    async with db_ready() as db:
        sets = await rules.list_rulesets(db)
        assert {s["name"] for s in sets} == {"Default", "Aggressive"}
        agg = await rules.get_ruleset_detail(db, res["ruleset_id"])
    assert len(agg["rules"]) == 4          # snapshot of Default's 4 rules
    assert agg["is_active"] is False       # save-as does not activate


@pytest.mark.asyncio
async def test_save_as_rejects_bad_names(db_ready):
    async with db_ready() as db:
        assert "error" in await rules.save_as(db, "", "t")
        assert "error" in await rules.save_as(db, "Default", "t")
        await rules.save_as(db, "Dup", "t")
        assert "error" in await rules.save_as(db, "Dup", "t")


@pytest.mark.asyncio
async def test_activate_and_restore_default(db_ready):
    async with db_ready() as db:
        saved = await rules.save_as(db, "Conservative", "t")
        await rules.activate(db, saved["ruleset_id"])
    async with db_ready() as db:
        assert (await rules.get_active_ruleset(db))["name"] == "Conservative"
    async with db_ready() as db:
        await rules.restore_default(db)
    async with db_ready() as db:
        active = await rules.get_active_ruleset(db)
        sets = await rules.list_rulesets(db)
    assert active["name"] == "Default"
    assert sum(1 for s in sets if s["is_active"]) == 1, "exactly one active rule-set"


@pytest.mark.asyncio
async def test_update_threshold_rule(db_ready):
    async with db_ready() as db:
        active = await rules.get_active_ruleset(db)
        rule = next(r for r in active["rules"]
                    if r["spec"]["setting"] == "max_priority_delta")
        res = await rules.update_rule(db, rule["id"], 5)
    assert res.get("ok") and res["value"] == 5
    async with db_ready() as db:
        active = await rules.get_active_ruleset(db)
        rule = next(r for r in active["rules"]
                    if r["spec"]["setting"] == "max_priority_delta")
    assert rule["spec"]["value"] == 5


@pytest.mark.asyncio
async def test_update_rule_rejects_bad_input(db_ready):
    async with db_ready() as db:
        active = await rules.get_active_ruleset(db)
        rid = active["rules"][0]["id"]
        assert "error" in await rules.update_rule(db, rid, "abc")
        assert "error" in await rules.update_rule(db, rid, -1)
        assert "error" in await rules.update_rule(db, "no-such-rule", 3)


@pytest.mark.asyncio
async def test_airi_tools_expose_rules(db_ready):
    async with db_ready() as db:
        await rules.ensure_seeded(db)
    out = await run_tool("get_rulesets", {})
    assert any(rs["name"] == "Default" for rs in out["rulesets"])
    out2 = await run_tool("get_active_rules", {})
    assert out2["name"] == "Default" and len(out2["rules"]) == 4
