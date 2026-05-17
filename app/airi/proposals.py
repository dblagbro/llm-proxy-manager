"""AIRI proposal service — v4.0 milestone 3.

The propose -> dry-run -> apply / revert flow. A proposal is created
``pending`` with its impact preview already attached; the operator either
approves it (apply) or AIRI applies it directly when the operator asked to
auto-apply. Every applied change snapshots prior state for one-click
revert, and the ``airi_proposal`` row IS the audit record.

Safety in this milestone:
  - the LLM never mutates directly — a propose tool only creates a pending
    proposal; apply is a separate, explicit step (a UI click, or an
    operator-requested auto-apply);
  - priority / auto-skip changes are capped by the **active rule-set's**
    threshold rules (this is what wires M2's rules to M3's actions);
  - every applied change is reversible from its ``prior_state`` snapshot.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, desc, func

from app.models.db import Provider, ActivityLog, AiriRule, AiriProposal
from app.airi import dryrun, rules

logger = logging.getLogger(__name__)

_PROVIDER_FIELDS = {"priority", "enabled", "auto_skip_hours"}
_TRAFFIC_WINDOW = 500  # recent requests sampled for the traffic-share preview


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── helpers ──────────────────────────────────────────────────────────────────

async def _resolve_provider(db, ref: str):
    """Resolve a provider by id, then exact name, then name-substring."""
    ref = (ref or "").strip()
    if not ref:
        return None
    p = await db.get(Provider, ref)
    if p is not None:
        return p
    rows = (await db.execute(
        select(Provider).where(Provider.deleted_at.is_(None))
    )).scalars().all()
    for p in rows:
        if (p.name or "").lower() == ref.lower():
            return p
    for p in rows:
        if ref.lower() in (p.name or "").lower():
            return p
    return None


async def _active_caps(db) -> dict:
    """Apply caps AIRI obeys — read from the active rule-set's threshold
    rules, so editing a rule (M2) actually constrains AIRI's actions (M3)."""
    caps = {"max_priority_delta": 2, "max_auto_skip_hours": 24}
    try:
        active = await rules.get_active_ruleset(db)
        for r in active.get("rules", []):
            s = r.get("spec") or {}
            if s.get("setting") in caps:
                caps[s["setting"]] = int(s.get("value"))
    except Exception:
        pass
    return caps


async def _provider_snapshot_and_traffic(db):
    """(providers, traffic_counts, total) for the dry-run preview."""
    rows = (await db.execute(
        select(Provider).where(Provider.deleted_at.is_(None))
    )).scalars().all()
    providers = [
        {"name": p.name, "priority": int(p.priority or 10), "enabled": bool(p.enabled)}
        for p in rows
    ]
    pmap = {p.id: p.name for p in rows}
    recent = (await db.execute(
        select(ActivityLog.provider_id)
        .where(ActivityLog.event_type == "llm_request")
        .order_by(desc(ActivityLog.created_at))
        .limit(_TRAFFIC_WINDOW)
    )).all()
    counts: dict = {}
    for (pid,) in recent:
        name = pmap.get(pid, pid or "?")
        counts[name] = counts.get(name, 0) + 1
    return providers, counts, len(recent)


def _proposal_dict(p: AiriProposal) -> dict:
    return {
        "id": p.id, "kind": p.kind, "target": p.target_label,
        "change": p.change or {}, "dry_run": p.dry_run or {},
        "status": p.status, "created_by": p.created_by,
        "created_via_prompt": p.created_via_prompt,
        "created_at": str(p.created_at) if p.created_at else None,
        "decided_at": str(p.decided_at) if p.decided_at else None,
        "decided_by": p.decided_by,
    }


# ── create proposals ─────────────────────────────────────────────────────────

async def create_provider_change(db, *, provider_ref: str, field: str, value,
                                  mode: str, created_by: str, prompt: str) -> dict:
    """Create a provider-change proposal (priority / enabled / auto_skip_hours)."""
    if field not in _PROVIDER_FIELDS:
        return {"error": f"unknown field '{field}' — one of {sorted(_PROVIDER_FIELDS)}"}
    p = await _resolve_provider(db, provider_ref)
    if p is None:
        return {"error": f"no provider matches '{provider_ref}'"}

    caps = await _active_caps(db)
    capped = False

    if field == "priority":
        cur = int(p.priority or 10)
        try:
            new = int(value)
        except (TypeError, ValueError):
            return {"error": "priority must be an integer"}
        cap = caps["max_priority_delta"]
        if abs(new - cur) > cap:
            new = cur + (cap if new > cur else -cap)
            capped = True
        from_v, to_v = cur, new
    elif field == "enabled":
        from_v = bool(p.enabled)
        to_v = value if isinstance(value, bool) else \
            str(value).strip().lower() in ("true", "1", "yes", "on", "enable", "enabled")
    elif field == "auto_skip_hours":
        from_v = 0
        try:
            to_v = int(value)
        except (TypeError, ValueError):
            return {"error": "auto_skip_hours must be an integer"}
        if to_v < 0:
            return {"error": "auto_skip_hours must be non-negative"}
        if to_v > caps["max_auto_skip_hours"]:
            to_v = caps["max_auto_skip_hours"]
            capped = True

    if from_v == to_v:
        return {"error": f"{p.name}'s {field} is already {to_v!r} — nothing to change"}

    providers, counts, total = await _provider_snapshot_and_traffic(db)
    impact = dryrun.provider_change_impact(
        field=field, target_name=p.name, current_value=from_v, new_value=to_v,
        providers=providers, traffic_counts=counts, traffic_total=total,
    )
    change = {"field": field, "from": from_v, "to": to_v, "capped": capped}
    if capped:
        change["cap_note"] = f"capped by the active rule-set (caps={caps})"

    prop = AiriProposal(
        id=secrets.token_hex(8), kind="provider_change", target_id=p.id,
        target_label=p.name, change=change, dry_run=impact, status="pending",
        created_by=created_by, created_via_prompt=prompt,
    )
    db.add(prop)
    await db.commit()
    return await _finish_create(db, prop, mode, created_by)


async def create_rule_change(db, *, rule_id: str, value, mode: str,
                             created_by: str, prompt: str) -> dict:
    """Create a threshold-rule change proposal."""
    r = await db.get(AiriRule, (rule_id or "").strip())
    if r is None:
        return {"error": f"no rule with id '{rule_id}'"}
    if r.kind != "threshold":
        return {"error": f"only threshold rules can be changed (this is '{r.kind}')"}
    try:
        new = int(value)
    except (TypeError, ValueError):
        return {"error": "value must be an integer"}
    if new < 0:
        return {"error": "value must be non-negative"}
    spec = r.spec or {}
    cur = int(spec.get("value", 0))
    if cur == new:
        return {"error": f"rule '{r.name}' is already {new}"}

    impact = dryrun.rule_change_impact(
        rule_name=r.name, setting=spec.get("setting", "?"),
        current_value=cur, new_value=new,
    )
    prop = AiriProposal(
        id=secrets.token_hex(8), kind="rule_change", target_id=r.id,
        target_label=r.name, change={"field": "value", "from": cur, "to": new},
        dry_run=impact, status="pending",
        created_by=created_by, created_via_prompt=prompt,
    )
    db.add(prop)
    await db.commit()
    return await _finish_create(db, prop, mode, created_by)


async def _finish_create(db, prop: AiriProposal, mode: str, created_by: str) -> dict:
    """Shared tail of create_* — return the proposal, auto-applying when the
    operator asked for it (mode == 'apply')."""
    result = {
        "proposal_id": prop.id, "kind": prop.kind, "target": prop.target_label,
        "change": prop.change, "dry_run": prop.dry_run, "status": "pending",
    }
    if mode == "apply":
        # Safety: auto-apply proceeds ONLY on a clean dry-run. A warning
        # (high traffic share, "leaves no providers", …) means a human must
        # look — the proposal stays pending for explicit operator approval.
        warnings = (prop.dry_run or {}).get("warnings") or []
        if warnings:
            result["apply_withheld"] = (
                "auto-apply withheld — the dry-run raised warnings; the "
                "proposal is pending your review and explicit approval."
            )
            return result
        applied = await apply_proposal(db, prop.id, applied_by=created_by)
        if "error" in applied:
            result["status"] = "pending"
            result["apply_error"] = applied["error"]
        else:
            result["status"] = "applied"
    return result


# ── decide proposals ─────────────────────────────────────────────────────────

async def apply_proposal(db, proposal_id: str, applied_by: str) -> dict:
    """Apply a pending proposal to live config, snapshotting prior state."""
    prop = await db.get(AiriProposal, proposal_id)
    if prop is None:
        return {"error": "proposal not found"}
    if prop.status != "pending":
        return {"error": f"proposal is '{prop.status}', not pending"}

    if prop.kind == "provider_change":
        p = await db.get(Provider, prop.target_id)
        if p is None:
            return {"error": "the provider no longer exists"}
        field, to = prop.change["field"], prop.change["to"]
        # Hard invariant — AIRI must never disable the last enabled
        # provider (that would take the whole proxy offline, including
        # AIRI's own LLM calls). Blocks even an explicit operator approve.
        if field == "enabled" and bool(to) is False and bool(p.enabled):
            others = (await db.execute(
                select(func.count()).select_from(Provider).where(
                    Provider.deleted_at.is_(None),
                    Provider.enabled == True,  # noqa: E712
                    Provider.id != p.id,
                )
            )).scalar() or 0
            if others == 0:
                return {"error": "refused — this would disable the last enabled "
                                  "provider; the proxy must keep at least one."}
        prop.prior_state = {
            "priority": p.priority,
            "enabled": bool(p.enabled),
            "auto_skip_until": p.auto_skip_until.isoformat() if p.auto_skip_until else None,
        }
        if field == "priority":
            p.priority = int(to)
        elif field == "enabled":
            p.enabled = bool(to)
        elif field == "auto_skip_hours":
            p.auto_skip_until = (_now() + timedelta(hours=int(to))) if int(to) > 0 else None
    elif prop.kind == "rule_change":
        r = await db.get(AiriRule, prop.target_id)
        if r is None:
            return {"error": "the rule no longer exists"}
        prop.prior_state = {"spec": dict(r.spec or {})}
        spec = dict(r.spec or {})
        spec["value"] = int(prop.change["to"])
        r.spec = spec
    else:
        return {"error": f"unknown proposal kind '{prop.kind}'"}

    prop.status = "applied"
    prop.decided_at = _now()
    prop.decided_by = applied_by
    await db.commit()
    logger.info("airi.proposal.applied id=%s kind=%s by=%s", prop.id, prop.kind, applied_by)
    return {"ok": True, "status": "applied", "proposal_id": prop.id}


async def reject_proposal(db, proposal_id: str, decided_by: str) -> dict:
    prop = await db.get(AiriProposal, proposal_id)
    if prop is None:
        return {"error": "proposal not found"}
    if prop.status != "pending":
        return {"error": f"proposal is '{prop.status}', not pending"}
    prop.status = "rejected"
    prop.decided_at = _now()
    prop.decided_by = decided_by
    await db.commit()
    return {"ok": True, "status": "rejected", "proposal_id": prop.id}


async def revert_proposal(db, proposal_id: str, decided_by: str) -> dict:
    """Undo an applied proposal — restore the prior_state snapshot."""
    prop = await db.get(AiriProposal, proposal_id)
    if prop is None:
        return {"error": "proposal not found"}
    if prop.status != "applied":
        return {"error": f"only an applied proposal can be reverted (this is '{prop.status}')"}
    prior = prop.prior_state or {}

    if prop.kind == "provider_change":
        p = await db.get(Provider, prop.target_id)
        if p is None:
            return {"error": "the provider no longer exists"}
        if "priority" in prior:
            p.priority = prior["priority"]
        if "enabled" in prior:
            p.enabled = bool(prior["enabled"])
        if "auto_skip_until" in prior:
            v = prior["auto_skip_until"]
            p.auto_skip_until = datetime.fromisoformat(v) if v else None
    elif prop.kind == "rule_change":
        r = await db.get(AiriRule, prop.target_id)
        if r is None:
            return {"error": "the rule no longer exists"}
        r.spec = dict(prior.get("spec") or {})

    prop.status = "reverted"
    prop.decided_at = _now()
    prop.decided_by = decided_by
    await db.commit()
    logger.info("airi.proposal.reverted id=%s by=%s", prop.id, decided_by)
    return {"ok": True, "status": "reverted", "proposal_id": prop.id}


# ── read ─────────────────────────────────────────────────────────────────────

async def list_proposals(db, status: str | None = None, limit: int = 25) -> list[dict]:
    q = select(AiriProposal).order_by(desc(AiriProposal.created_at)).limit(limit)
    if status:
        q = (select(AiriProposal).where(AiriProposal.status == status)
             .order_by(desc(AiriProposal.created_at)).limit(limit))
    rows = (await db.execute(q)).scalars().all()
    return [_proposal_dict(p) for p in rows]


async def get_proposal(db, proposal_id: str) -> dict | None:
    prop = await db.get(AiriProposal, proposal_id)
    return _proposal_dict(prop) if prop is not None else None
