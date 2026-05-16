"""AIRI rules layer — v4.0 milestone 2.

Operators organise the AI Provider Supervisor's policy as named, snapshot-able
rule-sets. This is the service layer: seed the ``Default`` set, list/read
rule-sets, save the current set under a new name, restore (activate) a set,
and edit a threshold rule.

Milestone 2: rules are **stored config**. They are wired to live supervisor
behaviour in a later milestone — editing a rule here does not yet change how
the supervisor runs.
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.db import AiriRuleset, AiriRule

logger = logging.getLogger(__name__)

DEFAULT_RULESET_NAME = "Default"


def _threshold_seed() -> list[dict]:
    """The threshold rules the Default rule-set is seeded with — a snapshot
    of the AI Provider Supervisor's current tunables."""
    return [
        {"name": "Scan interval (seconds)", "setting": "scan_interval_sec",
         "value": int(settings.ai_provider_supervisor_interval_sec)},
        {"name": "Short window (minutes)", "setting": "short_window_min",
         "value": int(settings.ai_provider_supervisor_short_window_min)},
        {"name": "Max priority delta", "setting": "max_priority_delta",
         "value": int(settings.ai_provider_supervisor_max_priority_delta)},
        {"name": "Max auto-skip (hours)", "setting": "max_auto_skip_hours",
         "value": int(settings.ai_provider_supervisor_max_auto_skip_hours)},
    ]


async def ensure_seeded(db) -> None:
    """Idempotently create the ``Default`` rule-set if it does not exist."""
    existing = (await db.execute(
        select(AiriRuleset).where(AiriRuleset.name == DEFAULT_RULESET_NAME)
    )).scalar_one_or_none()
    if existing is not None:
        return
    rs = AiriRuleset(
        id=secrets.token_hex(8), name=DEFAULT_RULESET_NAME,
        is_default=True, is_active=True,
        description="Seeded from the AI Provider Supervisor's settings.",
        created_by="system",
    )
    db.add(rs)
    for r in _threshold_seed():
        db.add(AiriRule(
            id=secrets.token_hex(8), ruleset_id=rs.id, name=r["name"],
            kind="threshold", spec={"setting": r["setting"], "value": r["value"]},
            mode="suggest", enabled=True, created_by="system",
        ))
    try:
        await db.commit()
        logger.info("airi.rules: seeded Default rule-set")
    except IntegrityError:
        # Lost a race — another worker seeded it concurrently. Fine.
        await db.rollback()


def _ruleset_summary(rs: AiriRuleset) -> dict:
    return {
        "id": rs.id, "name": rs.name,
        "is_default": bool(rs.is_default), "is_active": bool(rs.is_active),
        "description": rs.description, "created_by": rs.created_by,
        "updated_at": str(rs.updated_at) if rs.updated_at else None,
    }


def _rule_dict(r: AiriRule) -> dict:
    return {
        "id": r.id, "name": r.name, "kind": r.kind, "spec": r.spec or {},
        "mode": r.mode, "enabled": bool(r.enabled),
    }


async def list_rulesets(db) -> list[dict]:
    await ensure_seeded(db)
    rows = (await db.execute(
        select(AiriRuleset).order_by(AiriRuleset.is_default.desc(), AiriRuleset.name)
    )).scalars().all()
    return [_ruleset_summary(r) for r in rows]


async def get_ruleset_detail(db, ruleset_id: str) -> dict | None:
    await ensure_seeded(db)
    rs = await db.get(AiriRuleset, ruleset_id)
    if rs is None:
        return None
    rules = (await db.execute(
        select(AiriRule).where(AiriRule.ruleset_id == ruleset_id)
        .order_by(AiriRule.name)
    )).scalars().all()
    out = _ruleset_summary(rs)
    out["rules"] = [_rule_dict(r) for r in rules]
    return out


async def get_active_ruleset(db) -> dict:
    await ensure_seeded(db)
    rs = (await db.execute(
        select(AiriRuleset).where(AiriRuleset.is_active == True)  # noqa: E712
    )).scalar_one_or_none()
    if rs is None:
        # Nothing active — fall back to Default and activate it.
        rs = (await db.execute(
            select(AiriRuleset).where(AiriRuleset.name == DEFAULT_RULESET_NAME)
        )).scalar_one_or_none()
        if rs is not None:
            rs.is_active = True
            await db.commit()
    if rs is None:
        return {"error": "no rule-set available"}
    detail = await get_ruleset_detail(db, rs.id)
    return detail or {"error": "no rule-set available"}


async def save_as(db, name: str, created_by: str) -> dict:
    """Snapshot the active rule-set's rules into a new named rule-set. Does
    not change which set is active."""
    await ensure_seeded(db)
    name = (name or "").strip()
    if not name:
        return {"error": "a name is required"}
    if name == DEFAULT_RULESET_NAME:
        return {"error": "'Default' is a reserved name"}
    dup = (await db.execute(
        select(AiriRuleset).where(AiriRuleset.name == name)
    )).scalar_one_or_none()
    if dup is not None:
        return {"error": f"a rule-set named '{name}' already exists"}

    active = await get_active_ruleset(db)
    new = AiriRuleset(
        id=secrets.token_hex(8), name=name, is_default=False, is_active=False,
        description=f"Saved by {created_by}", created_by=created_by,
    )
    db.add(new)
    for r in active.get("rules", []):
        db.add(AiriRule(
            id=secrets.token_hex(8), ruleset_id=new.id, name=r["name"],
            kind=r["kind"], spec=dict(r.get("spec") or {}),
            mode=r.get("mode", "suggest"), enabled=bool(r.get("enabled", True)),
            created_by=created_by,
        ))
    await db.commit()
    return {"ok": True, "ruleset_id": new.id, "name": name}


async def activate(db, ruleset_id: str) -> dict:
    """Make ``ruleset_id`` the single active rule-set."""
    await ensure_seeded(db)
    target = await db.get(AiriRuleset, ruleset_id)
    if target is None:
        return {"error": "rule-set not found"}
    everyone = (await db.execute(select(AiriRuleset))).scalars().all()
    for rs in everyone:
        rs.is_active = (rs.id == ruleset_id)
    await db.commit()
    return {"ok": True, "active": target.name}


async def restore_default(db) -> dict:
    await ensure_seeded(db)
    rs = (await db.execute(
        select(AiriRuleset).where(AiriRuleset.name == DEFAULT_RULESET_NAME)
    )).scalar_one_or_none()
    if rs is None:
        return {"error": "Default rule-set is missing"}
    return await activate(db, rs.id)


async def update_rule(db, rule_id: str, value) -> dict:
    """Update a threshold rule's integer value (milestone 2 edits threshold
    rules only)."""
    await ensure_seeded(db)
    r = await db.get(AiriRule, rule_id)
    if r is None:
        return {"error": "rule not found"}
    if r.kind != "threshold":
        return {"error": f"milestone 2 can only edit threshold rules (this is '{r.kind}')"}
    try:
        ival = int(value)
    except (TypeError, ValueError):
        return {"error": "value must be an integer"}
    if ival < 0:
        return {"error": "value must be non-negative"}
    spec = dict(r.spec or {})
    spec["value"] = ival
    r.spec = spec
    await db.commit()
    return {"ok": True, "rule_id": rule_id, "value": ival}
