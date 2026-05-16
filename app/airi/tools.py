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
]

# Tool names AIRI is allowed to call (defence-in-depth — the agent loop
# checks this before dispatch).
READ_ONLY_TOOLS = frozenset(t["name"] for t in TOOL_SCHEMAS)


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
        return {"error": f"unknown tool: {name}"}
    except Exception as e:  # never raise out of a tool
        logger.warning("airi.tool_failed name=%s err=%r", name, e)
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
