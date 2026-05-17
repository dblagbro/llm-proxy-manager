"""AIRI read-only tools — v4.0 milestone 1.

AIRI grounds every factual claim about routing in real proxy state through
these tools. All are READ-ONLY — no tool mutates anything. Each opens its
own short-lived DB session and returns it promptly; no session is held
across the agent's LLM calls (the ARCH-A discipline).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, desc

from app.config import settings
from app.models.database import AsyncSessionLocal
from app.models.db import Provider, ActivityLog

logger = logging.getLogger(__name__)


# Anthropic tool-use schemas advertised to the model. Keep the set small
# and workflow-shaped (research: fewer, well-named tools route better).
TOOL_SCHEMAS = [
    {
        "name": "get_supervisor_state",
        "description": "Current state of the AI Provider Supervisor — enabled or not, "
                       "suggest vs auto-apply mode, the model it uses, scan interval, "
                       "and its safety caps.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_provider_health",
        "description": "Every provider with its routing-relevant state: name, type, "
                       "enabled, priority, and whether it is under a manual override "
                       "or an auto-skip.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_routing_config",
        "description": "Current routing configuration knobs — fallback, hedging, LMRH.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_routing",
        "description": "A sample of the most recent routing decisions — which provider "
                       "served each request and the outcome severity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "How many recent requests (default 20, max 100)."},
            },
        },
    },
    {
        "name": "get_rulesets",
        "description": "List the saved AIRI rule-sets — their names, which one is "
                       "the Default, and which is currently active.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_active_rules",
        "description": "The currently active rule-set and all of its rules — the "
                       "thresholds that govern the AI Provider Supervisor.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "explain_routing",
        "description": "A structured explanation of how the proxy's routing actually "
                       "works — priority ordering, LMRH hints, the circuit breaker, the "
                       "claude-oauth chain, hedging, fallback. Use for 'how does X work'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_conversations",
        "description": "Full-text search across EVERY operator's past AIRI "
                       "conversations — the shared change-coordination history. Use it "
                       "to recall an earlier discussion, or to check whether another "
                       "operator already discussed a provider before you propose a "
                       "change to it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Search terms — every term must appear."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_recent_changes",
        "description": "Recent AIRI proposals — the change audit trail: what was "
                       "proposed or applied, by which operator, and when. Check this "
                       "before proposing a provider change so you can flag a recent "
                       "change another operator already made to the same provider.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

# Tool names AIRI is allowed to call (defence-in-depth — the agent loop
# checks this before dispatch).
READ_ONLY_TOOLS = frozenset(t["name"] for t in TOOL_SCHEMAS)


# v4.0 milestone 3 — mutating "propose" tools. These never apply a change
# directly: they create a PENDING proposal (with a dry-run preview) that the
# operator approves, unless the operator explicitly asked AIRI to apply it.
PROPOSE_TOOL_SCHEMAS = [
    {
        "name": "propose_provider_change",
        "description": "Propose a change to a provider — its routing priority, its "
                       "enabled state, or a time-bounded auto-skip. Creates a PENDING "
                       "proposal with an impact preview; it is NOT applied until the "
                       "operator approves it. Set mode='apply' ONLY when the operator "
                       "explicitly asked you to apply or auto-apply the change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "Provider name or id."},
                "field": {"type": "string",
                          "enum": ["priority", "enabled", "auto_skip_hours"]},
                "value": {"description": "New value — integer for priority / "
                                         "auto_skip_hours, boolean for enabled."},
                "mode": {"type": "string", "enum": ["suggest", "apply"],
                         "description": "'suggest' (default) = pending proposal; "
                                        "'apply' = apply immediately. Use 'apply' "
                                        "only on an explicit operator request."},
            },
            "required": ["provider", "field", "value"],
        },
    },
    {
        "name": "propose_rule_change",
        "description": "Propose a new value for a threshold rule in the active "
                       "rule-set. Creates a PENDING proposal; not applied until the "
                       "operator approves it (or mode='apply' on explicit request). "
                       "Get rule ids from get_active_rules.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "description": "The rule's id."},
                "value": {"type": "integer", "description": "New integer value."},
                "mode": {"type": "string", "enum": ["suggest", "apply"]},
            },
            "required": ["rule_id", "value"],
        },
    },
    {
        "name": "propose_add_rule",
        "description": "Propose adding a scheduled rule to the active rule-set. A "
                       "'conditional' rule auto-skips a provider when its error rate "
                       "crosses a threshold; a 'monitor' rule only notifies the "
                       "operator. Creates a PENDING proposal — adding automation "
                       "always needs explicit operator approval. The recurring "
                       "evaluation is deterministic — no LLM runs on the schedule.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_type": {"type": "string", "enum": ["conditional", "monitor"]},
                "name": {"type": "string", "description": "A short name for the rule."},
                "provider": {"type": "string",
                             "description": "Provider name/id the rule watches."},
                "window_min": {"type": "integer",
                               "description": "Error-rate window, in minutes."},
                "op": {"type": "string", "enum": [">", ">=", "<", "<="]},
                "threshold": {"type": "number",
                              "description": "Error-rate percentage threshold."},
                "cadence_min": {"type": "integer",
                                "description": "How often to evaluate, in minutes."},
                "action_hours": {"type": "integer",
                                 "description": "conditional only — hours to "
                                                "auto-skip the provider when it fires."},
                "action_mode": {"type": "string", "enum": ["suggest", "auto_apply"],
                                "description": "conditional only — 'suggest' (the "
                                               "rule proposes the skip for approval) "
                                               "or 'auto_apply'. Use 'auto_apply' "
                                               "only on an explicit operator request."},
            },
            "required": ["rule_type", "name", "provider", "window_min", "op",
                         "threshold", "cadence_min"],
        },
    },
]

PROPOSE_TOOLS = frozenset(t["name"] for t in PROPOSE_TOOL_SCHEMAS)


async def run_tool(name: str, args: dict) -> dict:
    """Dispatch a tool call. Returns a JSON-serialisable dict. Never raises —
    a failure becomes an ``error`` field so the agent can recover."""
    try:
        if name == "get_supervisor_state":
            return _get_supervisor_state()
        if name == "get_provider_health":
            return await _get_provider_health()
        if name == "get_routing_config":
            return _get_routing_config()
        if name == "get_recent_routing":
            return await _get_recent_routing(_clamp_int(args.get("limit"), 20, 1, 100))
        if name == "get_rulesets":
            return await _get_rulesets()
        if name == "get_active_rules":
            return await _get_active_rules()
        if name == "explain_routing":
            return _explain_routing()
        if name == "search_conversations":
            return await _search_conversations(str(args.get("query") or ""))
        if name == "get_recent_changes":
            return await _get_recent_changes()
        return {"error": f"unknown tool: {name}"}
    except Exception as e:  # never raise out of a tool
        logger.warning("airi.tool_failed name=%s err=%r", name, e)
        return {"error": f"tool {name} failed: {e}"}


async def run_propose_tool(name: str, args: dict, *, actor: str, prompt: str) -> dict:
    """Dispatch a mutating 'propose' tool. Creates a pending proposal (and
    applies it only when ``mode='apply'``). Never raises."""
    from app.airi import proposals
    from app.models.database import AsyncSessionLocal

    mode = args.get("mode") or "suggest"
    if mode not in ("suggest", "apply"):
        mode = "suggest"
    try:
        async with AsyncSessionLocal() as db:
            if name == "propose_provider_change":
                return await proposals.create_provider_change(
                    db, provider_ref=str(args.get("provider") or ""),
                    field=str(args.get("field") or ""), value=args.get("value"),
                    mode=mode, created_by=actor or "operator", prompt=prompt or "",
                )
            if name == "propose_rule_change":
                return await proposals.create_rule_change(
                    db, rule_id=str(args.get("rule_id") or ""), value=args.get("value"),
                    mode=mode, created_by=actor or "operator", prompt=prompt or "",
                )
            if name == "propose_add_rule":
                return await proposals.create_add_rule(
                    db, rule_type=str(args.get("rule_type") or ""),
                    name=str(args.get("name") or ""),
                    provider_ref=str(args.get("provider") or ""),
                    window_min=args.get("window_min"), op=str(args.get("op") or ">"),
                    threshold=args.get("threshold"), cadence_min=args.get("cadence_min"),
                    action_hours=args.get("action_hours"),
                    action_mode=str(args.get("action_mode") or "suggest"),
                    created_by=actor or "operator", prompt=prompt or "",
                )
        return {"error": f"unknown propose tool: {name}"}
    except Exception as e:
        logger.warning("airi.propose_tool_failed name=%s err=%r", name, e)
        return {"error": f"tool {name} failed: {e}"}


def _clamp_int(v, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


def _as_naive(dt):
    return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt


def _get_supervisor_state() -> dict:
    return {
        "enabled": settings.ai_provider_supervisor_enabled,
        "mode": "auto-apply" if settings.ai_provider_supervisor_auto_apply else "suggest-only",
        "model": settings.ai_provider_supervisor_model,
        "scan_interval_sec": settings.ai_provider_supervisor_interval_sec,
        "short_window_min": settings.ai_provider_supervisor_short_window_min,
        "trend_window_days": settings.ai_provider_supervisor_trend_window_days,
        "caps": {
            "max_priority_delta": settings.ai_provider_supervisor_max_priority_delta,
            "max_auto_skip_hours": settings.ai_provider_supervisor_max_auto_skip_hours,
        },
    }


async def _get_provider_health() -> dict:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Provider)
            .where(Provider.deleted_at.is_(None))
            .order_by(Provider.priority)
        )).scalars().all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    providers = []
    for p in rows:
        sk = getattr(p, "auto_skip_until", None)
        providers.append({
            "name": p.name,
            "type": p.provider_type,
            "priority": p.priority,
            "enabled": bool(p.enabled),
            "manual_override": getattr(p, "manual_override_until", None) is not None,
            "auto_skipped": sk is not None and _as_naive(sk) > now,
        })
    return {"provider_count": len(providers), "providers": providers}


def _get_routing_config() -> dict:
    return {
        "fallback_enabled": getattr(settings, "fallback_enabled", None),
        "hedge_enabled": getattr(settings, "hedge_enabled", None),
        "lmrh_v2_enabled": getattr(settings, "lmrh_v2_enabled", None),
        "note": "Always-on routing behaviour: provider priority ordering, the "
                "circuit breaker, the claude-oauth provider chain, and cross-family "
                "(Anthropic<->OpenAI) translation. Call explain_routing for detail.",
    }


async def _get_recent_routing(limit: int) -> dict:
    async with AsyncSessionLocal() as db:
        pmap = {p.id: p.name for p in (await db.execute(select(Provider))).scalars().all()}
        rows = (await db.execute(
            select(ActivityLog.created_at, ActivityLog.provider_id, ActivityLog.severity)
            .where(ActivityLog.event_type == "llm_request")
            .order_by(desc(ActivityLog.created_at))
            .limit(limit)
        )).all()
    recent = [
        {"at": str(created_at), "provider": pmap.get(pid, pid or "?"), "severity": severity}
        for created_at, pid, severity in rows
    ]
    return {"count": len(recent), "recent": recent}


async def _get_rulesets() -> dict:
    from app.airi import rules
    async with AsyncSessionLocal() as db:
        return {"rulesets": await rules.list_rulesets(db)}


async def _get_active_rules() -> dict:
    from app.airi import rules
    async with AsyncSessionLocal() as db:
        return await rules.get_active_ruleset(db)


async def _search_conversations(query: str) -> dict:
    from app.airi import history
    async with AsyncSessionLocal() as db:
        results = await history.search_messages(db, query=query, limit=15)
    return {"query": query, "match_count": len(results), "results": results}


async def _get_recent_changes() -> dict:
    from app.airi import proposals
    async with AsyncSessionLocal() as db:
        recent = await proposals.list_proposals(db, limit=15)
    changes = [
        {"target": p.get("target"), "kind": p.get("kind"),
         "change": p.get("change"), "status": p.get("status"),
         "by": p.get("created_by"), "at": p.get("created_at")}
        for p in recent
    ]
    return {"count": len(changes), "recent_changes": changes}


def _explain_routing() -> dict:
    return {
        "title": "How llm-proxy2 routes a request",
        "steps": [
            "1. select_provider ranks enabled providers; a lower `priority` wins. "
            "Providers that are at-capacity (external rotation) or auto-skipped are "
            "filtered out first.",
            "2. LMRH hints on the request (cost, latency, region, cache, hedge) "
            "tighten the ranking — hard constraints exclude providers, soft hints "
            "reorder them.",
            "3. claude-oauth providers (Claude Pro Max subscriptions) short-circuit "
            "the pipeline: the request walks the claude-oauth chain; on a 401/403 it "
            "fails over to the next claude-oauth provider, then falls through to the "
            "litellm path.",
            "4. The circuit breaker opens a provider after repeated failures; an open "
            "provider is skipped until it half-opens.",
            "5. Fallback: if a provider fails, a non-streaming request retries down the "
            "ranked list. Streaming has no failover (it would break the SSE contract).",
            "6. Hedging (opt-in): when a TTFT signal suggests the primary may be slow, "
            "a backup stream is raced; the first stream with a healthy first chunk wins.",
            "7. Cross-family translation: an Anthropic-shaped request routed to an "
            "OpenAI provider (or vice versa) is translated automatically.",
        ],
        "supervisor": "The AI Provider Supervisor periodically reviews each provider's "
                      "recent stats with an LLM and can deprioritise or auto-skip a "
                      "degrading provider — suggest-only or auto-apply, with caps.",
    }
