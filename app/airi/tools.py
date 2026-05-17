"""AIRI read-only tools — v4.0 milestone 1.

AIRI grounds every factual claim about routing in real proxy state through
these tools. All are READ-ONLY — no tool mutates anything. Each opens its
own short-lived DB session and returns it promptly; no session is held
across the agent's LLM calls (the ARCH-A discipline).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, desc

from app.config import settings
from app.models.database import AsyncSessionLocal
from app.models.db import Provider, ActivityLog, ModelCapability

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
        "name": "get_error_summary",
        "description": "Aggregate digest of ERRORS in the activity log over a time "
                       "window — counts grouped by error class (rate_limit means "
                       "HTTP 429 / too-many-requests; timeout; upstream_5xx; auth; "
                       "bad_request; …), by provider, and by event type. Use this "
                       "FIRST for any 'are there errors / 429s / rate limits / "
                       "timeouts lately' question. Note: keepalive_probe rows are "
                       "background health checks, not real client traffic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "window_minutes": {"type": "integer",
                                   "description": "Look-back window in minutes (default 120)."},
            },
        },
    },
    {
        "name": "search_activity_log",
        "description": "Search the activity log — every recorded event (llm_request, "
                       "keepalive_probe, provider_test, usage_rotation, …) with its "
                       "severity, message and error detail. Filter by free text "
                       "(matches the message AND the error text — so query='429' or "
                       "'rate limit' or 'timeout' finds those errors), severity, "
                       "event type, provider, and a time window. This is how you "
                       "investigate an incident or confirm what happened.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Free-text match on the message + error detail "
                                          "(e.g. '429', 'rate limit', 'timeout')."},
                "errors_only": {"type": "boolean",
                                "description": "Shortcut — only warning/error/critical rows."},
                "severity": {"type": "string",
                             "enum": ["info", "warning", "error", "critical"]},
                "event_type": {"type": "string",
                               "description": "e.g. llm_request, keepalive_probe."},
                "provider": {"type": "string", "description": "Provider name filter."},
                "window_minutes": {"type": "integer",
                                   "description": "Look-back window in minutes (default 120)."},
                "limit": {"type": "integer",
                          "description": "Max rows returned (default 30, max 100)."},
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
        "description": "A structured explanation of how the proxy actually works — "
                       "priority ordering, LMRH hints, the circuit breaker, the "
                       "claude-oauth chain, hedging, fallback, AND the capability "
                       "adaptation layer (tool-call emulation, CoT emulation, vision "
                       "handling, caller memory). Use for 'how does X work' and for "
                       "'what happens to a request on a non-native provider'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_model_capabilities",
        "description": "Per-provider model capabilities: whether each provider's model "
                       "NATIVELY supports tool/function calling, reasoning (CoT) and "
                       "vision, plus its measured tool-call success rate. This tells "
                       "you exactly which providers emulate a capability vs do it "
                       "natively — use it to answer 'can provider X handle tools / "
                       "reasoning / images' or 'what happens to this request on "
                       "provider Y'. A capability the provider lacks is emulated by "
                       "the proxy (tools, CoT) or stripped (vision) — it is not a "
                       "hard failure.",
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
        if name == "get_error_summary":
            return await _get_error_summary(
                _clamp_int(args.get("window_minutes"), 120, 1, 10080))
        if name == "search_activity_log":
            return await _search_activity_log(args)
        if name == "get_rulesets":
            return await _get_rulesets()
        if name == "get_active_rules":
            return await _get_active_rules()
        if name == "explain_routing":
            return _explain_routing()
        if name == "get_model_capabilities":
            return await _get_model_capabilities()
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
            select(ActivityLog.created_at, ActivityLog.provider_id,
                   ActivityLog.severity, ActivityLog.event_meta)
            .where(ActivityLog.event_type == "llm_request")
            .order_by(desc(ActivityLog.created_at))
            .limit(limit)
        )).all()
    recent = []
    for created_at, pid, severity, meta in rows:
        row = {"at": str(created_at), "provider": pmap.get(pid, pid or "?"),
               "severity": severity}
        # surface the error class on non-info rows so the model sees *why*
        if severity != "info" and isinstance(meta, dict) and meta.get("error_class"):
            row["error_class"] = meta.get("error_class")
        recent.append(row)
    return {"count": len(recent), "recent": recent}


_ERROR_SEVERITIES = ("warning", "error", "critical")


def _now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _get_error_summary(window_minutes: int) -> dict:
    """Aggregate error digest — counts by error class, provider, event type."""
    cutoff = _now_naive() - timedelta(minutes=window_minutes)
    async with AsyncSessionLocal() as db:
        pmap = {p.id: p.name for p in (await db.execute(select(Provider))).scalars().all()}
        rows = (await db.execute(
            select(ActivityLog.provider_id, ActivityLog.event_type, ActivityLog.event_meta)
            .where(ActivityLog.created_at >= cutoff,
                   ActivityLog.severity.in_(_ERROR_SEVERITIES))
        )).all()
    by_class: dict = {}
    by_provider: dict = {}
    by_event_type: dict = {}
    for pid, event_type, meta in rows:
        meta = meta or {}
        ec = meta.get("error_class") or "unknown"
        by_class[ec] = by_class.get(ec, 0) + 1
        pname = pmap.get(pid) or meta.get("provider_name") or (pid or "?")
        by_provider[pname] = by_provider.get(pname, 0) + 1
        by_event_type[event_type] = by_event_type.get(event_type, 0) + 1

    def _sorted(d):
        return dict(sorted(d.items(), key=lambda kv: -kv[1]))

    return {
        "window_minutes": window_minutes,
        "total_errors": len(rows),
        "by_error_class": _sorted(by_class),
        "by_provider": _sorted(by_provider),
        "by_event_type": _sorted(by_event_type),
        "note": "rate_limit == HTTP 429. keepalive_probe rows are background "
                "health checks, not client traffic.",
    }


async def _search_activity_log(args: dict) -> dict:
    """Filtered search over the activity log — text, severity, type, provider."""
    window = _clamp_int(args.get("window_minutes"), 120, 1, 10080)
    limit = _clamp_int(args.get("limit"), 30, 1, 100)
    cutoff = _now_naive() - timedelta(minutes=window)
    async with AsyncSessionLocal() as db:
        pmap = {p.id: p.name for p in (await db.execute(select(Provider))).scalars().all()}
        stmt = select(ActivityLog).where(ActivityLog.created_at >= cutoff)
        sev = args.get("severity")
        if sev in ("info", "warning", "error", "critical"):
            stmt = stmt.where(ActivityLog.severity == sev)
        elif args.get("errors_only"):
            stmt = stmt.where(ActivityLog.severity.in_(_ERROR_SEVERITIES))
        et = (args.get("event_type") or "").strip()
        if et:
            stmt = stmt.where(ActivityLog.event_type == et)
        # over-fetch — the free-text / provider filters are applied in Python
        stmt = stmt.order_by(desc(ActivityLog.created_at)).limit(limit * 5)
        rows = (await db.execute(stmt)).scalars().all()

    q = (args.get("query") or "").strip().lower()
    prov = (args.get("provider") or "").strip().lower()
    events = []
    for r in rows:
        meta = r.event_meta if isinstance(r.event_meta, dict) else {}
        pname = pmap.get(r.provider_id) or meta.get("provider_name") or r.provider_id
        if prov and prov not in (pname or "").lower():
            continue
        if q:
            blob = (json.dumps(meta, default=str) + " " + (r.message or "")).lower()
            if q not in blob:
                continue
        events.append({
            "at": str(r.created_at), "event_type": r.event_type,
            "severity": r.severity, "provider": pname,
            "message": (r.message or "")[:200],
            "error": (meta.get("error") or "")[:300] or None,
            "error_class": meta.get("error_class"),
        })
        if len(events) >= limit:
            break
    return {"window_minutes": window, "match_count": len(events), "events": events}


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
        "title": "How llm-proxy2 routes and adapts a request",
        "routing_steps": [
            "1. select_provider ranks enabled providers by `priority` (lower wins). "
            "At-capacity (external rotation) and auto-skipped providers are filtered out.",
            "2. LMRH hints (cost, latency, region, cache, hedge, task) tighten the "
            "ranking — hard constraints exclude providers, soft hints reorder them.",
            "3. claude-oauth providers (Claude Pro Max subscriptions) short-circuit the "
            "pipeline: the request walks the claude-oauth chain; a 401/403 fails over to "
            "the next claude-oauth provider, then through to the litellm path.",
            "4. The circuit breaker opens a provider after repeated failures; an open "
            "provider is skipped until it half-opens.",
            "5. Fallback: a non-streaming request that fails retries down the ranked "
            "list. A streaming request has no mid-stream failover (the SSE contract), "
            "but it is pre-flighted so a pre-stream failure still falls back.",
            "6. Hedging (opt-in): when TTFT telemetry suggests the primary is slow, a "
            "backup stream is raced; the first stream with a healthy first chunk wins.",
        ],
        "adaptation_layer": {
            "principle": "Cross-emulate, don't fail — a provider that lacks a "
                         "capability the request needs has it EMULATED by the proxy, so "
                         "any model can serve another model's request.",
            "tool_calling": "If a request carries tools and the chosen provider is not "
                            "native-tool-capable, the proxy injects the tool schemas as "
                            "a system prompt, parses <tool_call> blocks from the reply, "
                            "and emits real tool_use back to the client — in BOTH "
                            "Anthropic and OpenAI wire formats, INCLUDING streaming SSE. "
                            "Parallel tool calls and multi-turn tool_result history are "
                            "handled. Engages automatically.",
            "reasoning_cot": "If the provider lacks native reasoning, chain-of-thought "
                             "is emulated (the app/cot pipeline) when the caller is a "
                             "claude-code key or sends an LMRH task=reasoning hint. "
                             "cot_enabled is on by default.",
            "vision": "If a request has images and the provider is not vision-capable, "
                      "the images are STRIPPED and the request proceeds text-only — "
                      "the one lossy adaptation; it is surfaced on the response.",
            "memory": "Caller memory (app/memory) injects prior context per API key "
                      "when that key has opted in.",
            "translation": "An Anthropic-shaped request on an OpenAI provider (or vice "
                            "versa) is translated automatically; if no same-family "
                            "model matches, the provider's default model is substituted "
                            "and the original is reported in X-Substituted-From.",
            "observability": "Every response carries X-Emulation-Level "
                             "(minimal / standard / enhanced) and an LLM-Capability "
                             "header naming what was emulated or left unmet.",
        },
        "residual_gaps": [
            "Vision is stripped, not translated — a non-vision provider loses the images.",
            "Tool emulation does NOT engage when CoT is engaged on the same request — "
            "they are mutually exclusive in the router.",
            "Tool emulation depends on the model emitting well-formed <tool_call> "
            "blocks; a weak model may answer in prose instead — a soft degradation "
            "tracked by tool_call_success_rate, not a crash or a broken stream.",
            "Anthropic cache_control directives are dropped on non-Anthropic providers "
            "(a caching/perf hint, not a correctness issue).",
        ],
        "supervisor": "The AI Provider Supervisor periodically reviews each provider's "
                      "recent stats with an LLM and can deprioritise or auto-skip a "
                      "degrading provider — suggest-only or auto-apply, with caps.",
        "for_capability_questions": "Call get_model_capabilities for the per-provider "
                                    "native-vs-emulated breakdown.",
    }


async def _get_model_capabilities() -> dict:
    """Per-provider native_tools / native_reasoning / native_vision — so the
    model can say exactly which providers emulate a capability vs do it natively."""
    async with AsyncSessionLocal() as db:
        provs = {p.id: p for p in (await db.execute(
            select(Provider).where(Provider.deleted_at.is_(None)))).scalars().all()}
        caps = (await db.execute(
            select(ModelCapability).where(ModelCapability.deleted_at.is_(None)))
        ).scalars().all()
    models = []
    for c in caps:
        p = provs.get(c.provider_id)
        if p is None:
            continue
        models.append({
            "provider": p.name,
            "model": c.model_id,
            "native_tools": bool(c.native_tools),
            "native_reasoning": bool(c.native_reasoning),
            "native_vision": bool(c.native_vision),
            "tool_call_success_rate": c.tool_call_success_rate,
            "adaptation": "; ".join([
                "tools native" if c.native_tools
                else "tools EMULATED (proxy injects tool prompts)",
                "reasoning native" if c.native_reasoning
                else "reasoning EMULATED via CoT when engaged",
                "vision native" if c.native_vision
                else "vision STRIPPED (images dropped)",
            ]),
        })
    models.sort(key=lambda r: (r["provider"], r["model"]))
    return {
        "count": len(models),
        "models": models,
        "note": "A non-native tool or reasoning capability is EMULATED by the proxy — "
                "the request is adapted, not failed. Vision is the exception: images "
                "are stripped for a non-vision provider.",
    }
