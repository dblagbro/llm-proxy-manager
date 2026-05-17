"""AIRI rule evaluator — v4.0 milestone 4.

The deterministic, no-LLM background loop. It evaluates the active
rule-set's ``conditional`` and ``monitor`` rules on a cadence:

  - a conditional rule that fires takes its action (auto-skip a provider)
    through the M3 proposal machinery, so every apply guard — caps, the
    last-provider invariant, dry-run-warning blocking, audit, revert —
    still applies;
  - a monitor rule that fires notifies the operator.

Keystone safety property: **no LLM runs in this loop.** The LLM authored
each rule once, with operator approval; recurring evaluation is plain
Python. Per-rule cooldown + a runs-per-hour cap (the oscillation breaker)
+ the ``airi_automation_enabled`` kill switch bound it.

ARCH-A discipline: the DB session is never held across a notification
send (SMTP) — the loop collects notes and sends them after the session
closes.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.models.db import ActivityLog, AiriRule
from app.airi.notify import airi_notify

logger = logging.getLogger(__name__)

_DEFAULT_COOLDOWN_SEC = 600
_DEFAULT_MAX_RUNS_PER_HOUR = 5
_OPS = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _provider_error_rate(db, provider_id: str, window_min: int):
    """(total, errors, pct) for a provider over the window. Excludes
    internal-source traffic (BUG-026). pct is None when total == 0."""
    cutoff = _now() - timedelta(minutes=int(window_min or 30))
    rows = (await db.execute(
        select(ActivityLog.severity, ActivityLog.event_meta)
        .where(ActivityLog.event_type == "llm_request")
        .where(ActivityLog.provider_id == provider_id)
        .where(ActivityLog.created_at >= cutoff)
    )).all()
    total = errors = 0
    for severity, meta in rows:
        m = meta if isinstance(meta, dict) else {}
        if m.get("internal_source"):
            continue
        total += 1
        if severity in ("error", "critical"):
            errors += 1
    pct = (100.0 * errors / total) if total else None
    return total, errors, pct


async def evaluate_condition(db, condition: dict):
    """Return ``(fired: bool, observed, detail)``. Milestone 4 supports the
    ``error_rate_pct`` metric only."""
    condition = condition or {}
    if condition.get("metric") != "error_rate_pct":
        return False, None, f"unsupported metric {condition.get('metric')!r}"
    pid = condition.get("provider_id")
    if not pid:
        return False, None, "condition has no provider_id"
    op = _OPS.get(condition.get("op", ">"))
    if op is None:
        return False, None, f"unsupported operator {condition.get('op')!r}"
    total, _errors, pct = await _provider_error_rate(
        db, pid, condition.get("window_min", 30))
    if pct is None:
        return False, None, "no traffic in the window"
    fired = op(pct, float(condition.get("value", 0)))
    detail = (f"error rate {pct:.1f}% over {total} requests "
              f"(threshold {condition.get('op')} {condition.get('value')}%)")
    return fired, pct, detail


async def _act_conditional(db, rule: AiriRule, spec: dict, detail: str):
    """Apply a fired conditional rule's action — with cooldown + the
    oscillation breaker. Returns ``(outcome, note_or_None)`` where note is
    ``(subject, message, severity)`` to send after the session closes."""
    from app.airi import proposals
    now = _now()
    osc = dict(rule.oscillation_state or {})
    acts = []
    for t in osc.get("acts", []):
        try:
            acts.append(datetime.fromisoformat(t))
        except (TypeError, ValueError):
            pass
    acts = [t for t in acts if t > now - timedelta(hours=1)]

    cooldown = int(rule.cooldown_sec or _DEFAULT_COOLDOWN_SEC)
    if acts and (now - max(acts)).total_seconds() < cooldown:
        rule.last_action = f"cooled down ({detail})"
        return "cooled_down", None

    cap = int(rule.max_runs_per_window or _DEFAULT_MAX_RUNS_PER_HOUR)
    if len(acts) >= cap:
        # Oscillation breaker — the rule has acted too often; disable it.
        rule.enabled = False
        rule.last_action = "tripped — oscillation breaker"
        rule.oscillation_state = {"acts": [t.isoformat() for t in acts],
                                  "tripped_at": now.isoformat()}
        return "tripped", (
            f"rule '{rule.name}' DISABLED — oscillation breaker",
            f"Scheduled rule '{rule.name}' acted {len(acts)} times in the last "
            f"hour (cap {cap}) and has been automatically disabled. Review it "
            f"in AIRI before re-enabling.",
            "error",
            "automation",
        )

    action = spec.get("action") or {}
    if action.get("type") != "auto_skip":
        rule.last_action = f"skipped — unsupported action {action.get('type')!r}"
        return "acted", None

    hours = int(action.get("hours") or 1)
    pid = (spec.get("condition") or {}).get("provider_id")
    res = await proposals.create_provider_change(
        db, provider_ref=pid, field="auto_skip_hours", value=hours,
        mode=("apply" if rule.mode == "auto_apply" else "suggest"),
        created_by=f"airi-scheduled-rule:{rule.name}",
        prompt=f"scheduled rule '{rule.name}' fired — {detail}",
    )
    acts.append(now)
    rule.oscillation_state = {"acts": [t.isoformat() for t in acts]}
    status = res.get("status", "pending")
    rule.last_action = f"{status} — auto_skip {hours}h ({detail})"
    if status == "applied":
        note = (f"rule '{rule.name}' auto-skipped a provider",
                f"Scheduled rule '{rule.name}' fired ({detail}) and auto-skipped "
                f"the provider for {hours}h.", "warning", "automation")
    else:
        note = (f"rule '{rule.name}' raised a pending proposal",
                f"Scheduled rule '{rule.name}' fired ({detail}). A proposal to "
                f"auto-skip the provider for {hours}h is PENDING your approval.",
                "warning", "automation")
    return "acted", note


async def evaluate_due_rules(db):
    """One evaluator pass. Returns ``(summary, notifications)`` — the caller
    sends the notifications after the DB session is closed."""
    from app.airi import rules
    summary = {"evaluated": 0, "fired": 0, "acted": 0, "notified": 0,
               "cooled_down": 0, "tripped": 0}
    notifications: list[tuple] = []
    now = _now()

    active = await rules.get_active_ruleset(db)
    for rd in active.get("rules", []):
        if rd.get("kind") not in ("conditional", "monitor"):
            continue
        rule = await db.get(AiriRule, rd["id"])
        if rule is None or not rule.enabled:
            continue
        if rule.expiry_at and rule.expiry_at <= now:
            continue
        spec = rule.spec or {}
        cadence = int(spec.get("cadence_min") or 0)
        if rule.last_run_at and cadence > 0:
            if rule.last_run_at + timedelta(minutes=cadence) > now:
                continue  # not due yet

        summary["evaluated"] += 1
        fired, _observed, detail = await evaluate_condition(db, spec.get("condition"))
        rule.last_run_at = now
        if not fired:
            await db.commit()
            continue

        summary["fired"] += 1
        if rule.kind == "monitor":
            rule.last_action = f"fired — {detail}"
            await db.commit()
            notifications.append((
                f"monitor '{rule.name}' fired",
                f"Monitor rule '{rule.name}': {detail}.",
                "warning",
                "monitor",
            ))
            summary["notified"] += 1
            continue

        outcome, note = await _act_conditional(db, rule, spec, detail)
        summary[outcome] = summary.get(outcome, 0) + 1
        await db.commit()
        if note:
            notifications.append(note)

    return summary, notifications


# ── kill switch ──────────────────────────────────────────────────────────────
# A runtime override on top of the airi_automation_enabled env default.
# None = follow the env setting; True/False = explicit operator override.
# Resets to the env default on restart (fail-safe — automation off by default).
_automation_override: "bool | None" = None


def is_automation_enabled() -> bool:
    if _automation_override is not None:
        return _automation_override
    return bool(getattr(settings, "airi_automation_enabled", False))


def set_automation(enabled: bool) -> None:
    global _automation_override
    _automation_override = bool(enabled)


# ── background loop ──────────────────────────────────────────────────────────

_task: "asyncio.Task | None" = None


async def _loop() -> None:
    from app.models.database import AsyncSessionLocal
    await asyncio.sleep(20)  # boot delay — don't fight startup
    while True:
        interval = max(15, int(getattr(settings, "airi_evaluator_interval_sec", 60)))
        try:
            if settings.airi_enabled and is_automation_enabled():
                async with AsyncSessionLocal() as db:
                    _summary, notes = await evaluate_due_rules(db)
                # ARCH-A — notify only AFTER the session is closed.
                for subject, message, severity, category in notes:
                    await airi_notify(subject, message, severity, category)
        except Exception as e:
            logger.warning("airi.evaluator tick failed: %r", e)
        await asyncio.sleep(interval)


def start() -> None:
    """Idempotent start — called from the app lifespan."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        _task = asyncio.get_event_loop().create_task(_loop())
        logger.info("airi.evaluator started")
    except Exception as e:
        logger.warning("airi.evaluator failed to start: %r", e)
