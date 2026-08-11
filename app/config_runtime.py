"""
Runtime settings — DB-backed overrides on top of env-var defaults.

Keys, types, and human-readable labels for every tunable setting.
`load(db)` is called on startup and after each PUT /api/settings.
`apply(overrides)` patches the shared `settings` singleton in-place so
all existing code that reads `from app.config import settings` picks up
the change without modification.

v3.0.8 (item 4): the canonical type for each setting is the pydantic
field's annotation on ``app.config.Settings``. SCHEMA's ``type`` field
is an *input hint* for the UI (text vs number vs checkbox) and is only
trusted when no matching pydantic field exists. ``_coerce`` for a
recognised field always uses the pydantic type so coercion never drifts
from what the rest of the app reads. This prevents the v3.0.1 class of
bug where SCHEMA said ``"str"`` for a float field, ``_coerce`` returned
the raw string, and ``settings.shadow_traffic_rate > 0`` raised
TypeError on every successful non-streaming /v1/messages call.

A boot-time consistency check logs a WARNING for any SCHEMA entry whose
declared ``type`` disagrees with the pydantic field's type. Operators
see the discrepancy in logs and can fix the SCHEMA entry without
needing a production incident first.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings

logger = logging.getLogger(__name__)


# ── pydantic-type derivation (canonical) ────────────────────────────────────


def _pydantic_field_type(key: str) -> Optional[str]:
    """Return the pydantic field's type as one of {bool,int,float,str},
    or None if no field with that name exists. Pulls from the model's
    field-info — ``Settings`` is a Pydantic v2 BaseSettings.
    """
    fields = getattr(type(settings), "model_fields", None)
    if not fields:
        return None
    field = fields.get(key)
    if field is None:
        return None
    # Pydantic v2: field.annotation is the actual type
    ann = field.annotation
    # Unwrap Optional[X] (Union[X, None])
    origin = getattr(ann, "__origin__", None)
    if origin is not None:
        args = getattr(ann, "__args__", ())
        non_none = [a for a in args if a is not type(None)]  # noqa: E721
        if len(non_none) == 1:
            ann = non_none[0]
    if ann is bool:
        return "bool"
    if ann is int:
        return "int"
    if ann is float:
        return "float"
    if ann is str:
        return "str"
    return None


def canonical_type(key: str, schema_meta: dict) -> str:
    """Return the type to use for ``_coerce`` on this key.

    Priority:
      1. Pydantic field's annotation (canonical — what the rest of the
         app reads via ``settings.<key>``).
      2. ``schema_meta["type"]`` (fallback — for keys without a matching
         pydantic field, which are rare but possible).
      3. ``"str"`` (last resort — leaves the value unchanged).
    """
    pyd = _pydantic_field_type(key)
    if pyd is not None:
        return pyd
    return schema_meta.get("type", "str")


def validate_schema_consistency() -> list[str]:
    """Boot-time audit: warn for any SCHEMA entry whose declared type
    disagrees with the pydantic field type. Returns the list of
    mismatch descriptions (also logged as warnings)."""
    mismatches: list[str] = []
    for key, meta in SCHEMA.items():
        declared = meta.get("type", "str")
        pyd = _pydantic_field_type(key)
        if pyd is None:
            continue  # No pydantic field — schema is the only source
        if declared != pyd:
            msg = (f"config_runtime.SCHEMA['{key}'].type='{declared}' but "
                   f"pydantic settings.{key} is '{pyd}' — using pydantic "
                   "for coercion (canonical). Update SCHEMA to match.")
            mismatches.append(msg)
            logger.warning(msg)
    return mismatches


SCHEMA: dict[str, dict] = {
    # LMRHv2 (v3.3.0+) — bidirectional metrics feedback. Default-off
    # per operator decision #6; flip per-node when ready.
    "lmrh_v2_enabled":        {"type": "bool",  "default": False, "label": "Enable LMRH v2 endpoints (/lmrh/providers, /lmrh/health, /.well-known/lmrh-config)"},
    # CoT-E
    "cot_enabled":            {"type": "bool",  "default": True,  "label": "Enable CoT-E globally"},
    "cot_max_iterations":     {"type": "int",   "default": settings.cot_max_iterations,     "label": "Max refinement passes"},
    "cot_quality_threshold":  {"type": "int",   "default": settings.cot_quality_threshold,  "label": "Quality threshold (1–10)"},
    "cot_critique_max_tokens":{"type": "int",   "default": settings.cot_critique_max_tokens,"label": "Critique max tokens"},
    "cot_plan_max_tokens":    {"type": "int",   "default": settings.cot_plan_max_tokens,    "label": "Plan max tokens"},
    "cot_min_tokens_skip":    {"type": "int",   "default": settings.cot_min_tokens_skip,    "label": "Min draft tokens to skip refinement"},
    "cot_verify_enabled":     {"type": "bool",  "default": settings.cot_verify_enabled,     "label": "Enable verification pass"},
    "cot_verify_max_tokens":  {"type": "int",   "default": settings.cot_verify_max_tokens,  "label": "Verification max tokens"},
    "cot_verify_auto_detect": {"type": "bool",  "default": settings.cot_verify_auto_detect, "label": "Auto-detect shell/infra commands"},
    "cot_cross_provider_critique": {"type": "bool", "default": settings.cot_cross_provider_critique, "label": "Route critique to a different provider than the draft (eliminates self-preference bias)"},
    "cot_verify_execute": {"type": "bool", "default": settings.cot_verify_execute, "label": "Actually execute the network-safe subset of verify steps (HTTP/DNS/TCP only)"},
    "cot_verify_step_timeout_sec": {"type": "float", "default": settings.cot_verify_step_timeout_sec, "label": "Per-step verify execution timeout (seconds)"},
    "cot_plan_compact": {"type": "bool", "default": settings.cot_plan_compact, "label": "Chain-of-Draft plan: ~5-word mini-steps (-78% plan tokens, faster TTFT)"},
    "fallback_enabled": {"type": "bool", "default": settings.fallback_enabled, "label": "Ordered fallback: on provider failure, try next-ranked candidate"},
    "fallback_max_providers": {"type": "int", "default": settings.fallback_max_providers, "label": "Max providers to try per request before giving up"},
    "task_auto_detect_enabled": {"type": "bool", "default": settings.task_auto_detect_enabled, "label": "Auto-classify LMRH task= hint via embedding cosine (~40ms overhead)"},
    "shadow_traffic_rate": {"type": "float", "default": settings.shadow_traffic_rate, "label": "Shadow-traffic fraction (0.0–1.0); 0.01 = mirror 1% of requests"},
    "shadow_candidate_provider_id": {"type": "str", "default": settings.shadow_candidate_provider_id, "label": "Provider ID to shadow-test"},
    "structured_output_enabled": {"type": "bool", "default": settings.structured_output_enabled, "label": "Enforce JSON-Schema response_format via repair loop"},
    "structured_output_max_repairs": {"type": "int", "default": settings.structured_output_max_repairs, "label": "Max structured-output repair attempts (default 2)"},
    "vision_route_enabled": {"type": "bool", "default": settings.vision_route_enabled, "label": "Vision-to-text: route images through VLM instead of stripping"},
    # Semantic cache (Wave 1 #3)
    "semantic_cache_enabled":          {"type": "bool", "default": settings.semantic_cache_enabled,         "label": "Enable semantic cache globally"},
    "semantic_cache_threshold":        {"type": "float",  "default": settings.semantic_cache_threshold,  "label": "Cosine threshold (0.0–1.0)"},
    "semantic_cache_ttl_sec":          {"type": "int",  "default": settings.semantic_cache_ttl_sec,         "label": "TTL (seconds)"},
    "semantic_cache_min_response_chars":{"type":"int",  "default": settings.semantic_cache_min_response_chars,"label":"Min response chars to cache"},
    # v3.0.67: route embeddings through a specific proxy provider (e.g. priority=1 Google)
    # v3.7.16 — was "string"; pydantic side reports "str". Mismatch
    # fired a warning on every settings load; harmonized here.
    "semantic_cache_embedding_model":  {"type": "str", "default": settings.semantic_cache_embedding_model, "label": "Embedding model (e.g. text-embedding-3-small, gemini-embedding-001)"},
    "semantic_cache_provider_id":      {"type": "str", "default": settings.semantic_cache_provider_id,    "label": "Pin embeddings to this provider id (blank = let litellm pick from model name)"},
    # Hedged requests (Wave 1 #4)
    "hedge_enabled":     {"type": "bool", "default": settings.hedge_enabled,     "label": "Enable hedged requests globally"},
    "hedge_max_per_sec": {"type": "float",  "default": settings.hedge_max_per_sec, "label": "Max hedge requests per second (global bucket)"},
    # Native reasoning
    "native_thinking_budget_tokens": {"type": "int", "default": settings.native_thinking_budget_tokens, "label": "Thinking budget tokens (Gemini 2.5 / Anthropic)"},
    "native_reasoning_effort":       {"type": "str", "default": settings.native_reasoning_effort,       "label": "Reasoning effort (o-series: low / medium / high)"},
    # Circuit breaker
    # v2.8.4 — activity-log payload capture
    # v3.0.94 — previews split out from full bodies
    "activity_log_capture_previews": {
        "type": "bool", "default": settings.activity_log_capture_previews,
        "label": "Activity log: capture short message previews (~240 chars each, ~500 bytes per row). Lightweight; safe to leave on.",
        "group": "Activity log",
    },
    "activity_log_capture_bodies": {
        "type": "bool", "default": settings.activity_log_capture_bodies,
        "label": "Activity log: capture FULL request + response payloads (heavyweight; ~5-100KB each row). Default OFF after 2026-05-06 pool-exhaustion incident — only enable for active wire-debugging.",
        "group": "Activity log",
    },
    "activity_log_max_body_chars": {
        "type": "int", "default": settings.activity_log_max_body_chars,
        "label": "Activity log: max characters per full body (only relevant when capture_bodies is on; default 4000).",
        "group": "Activity log",
    },
    "activity_log_body_sample_rate_4xx": {
        "type": "float", "default": settings.activity_log_body_sample_rate_4xx,
        "label": "Activity log: sample rate (0.0-1.0) for full request_body capture on bad_request 4xx upstream rejections. Independent of activity_log_capture_bodies. Default 0.01 = 1%.",
        "group": "Activity log",
    },
    "circuit_breaker_threshold":    {"type": "int", "default": settings.circuit_breaker_threshold,    "label": "CB failure threshold"},
    "circuit_breaker_timeout_sec":  {"type": "int", "default": settings.circuit_breaker_timeout_sec,  "label": "CB timeout (seconds)"},
    "circuit_breaker_halfopen_sec": {"type": "int", "default": settings.circuit_breaker_halfopen_sec, "label": "CB half-open window (seconds)"},
    "circuit_breaker_success_needed":{"type":"int", "default": settings.circuit_breaker_success_needed,"label": "CB successes needed to close"},
    "hold_down_sec":                {"type": "int", "default": settings.hold_down_sec,                "label": "Provider hold-down (seconds)"},
    # ── Run runtime (v3.0 / R6 lock-in) ──────────────────────────────────────
    "runs_max_turns_ceiling": {
        "type": "int",
        "default": getattr(settings, "runs_max_turns_ceiling", 50),
        "label": "Run runtime: max-turns admin ceiling (default 50, hard 200)",
        "group": "Run runtime",
    },
    "runs_max_model_calls_per_minute": {
        "type": "int",
        "default": getattr(settings, "runs_max_model_calls_per_minute", 5),
        "label": "Run runtime: per-Run model calls per minute (rate limit)",
        "group": "Run runtime",
    },
    "keepalive_probe_interval_sec": {
        "type": "int",
        "default": getattr(settings, "keepalive_probe_interval_sec", 300),
        "label": "Keep-alive probes: synthetic 'Hi from <ProviderName>' interval (seconds; 0 to disable)",
        "group": "Run runtime",
    },
    "activity_log_retention_days": {
        "type": "int",
        "default": getattr(settings, "activity_log_retention_days", 30),
        "label": "Activity log + provider_metrics + run_events retention (days)",
        "group": "Activity log",
    },
    # SMTP
    "smtp_enabled": {"type": "bool",  "default": settings.smtp_enabled, "label": "Enable email alerts"},
    "smtp_host":    {"type": "str",   "default": settings.smtp_host or "",    "label": "SMTP host"},
    "smtp_port":    {"type": "int",   "default": settings.smtp_port,         "label": "SMTP port"},
    "smtp_from":    {"type": "str",   "default": settings.smtp_from or "",   "label": "From address"},
    "smtp_to":      {"type": "str",   "default": settings.smtp_to or "",     "label": "Alert recipient"},
    "smtp_user":    {"type": "str",   "default": settings.smtp_user or "",   "label": "SMTP username"},
    "smtp_pass":    {"type": "str",   "default": settings.smtp_pass or "",   "label": "SMTP password"},
    "smtp_helo":    {"type": "str",   "default": settings.smtp_helo or "",   "label": "SMTP HELO/EHLO name"},
    # ── Wave 6 — Audit log export ────────────────────────────────────────────
    "audit_export_s3_bucket": {
        "type": "str", "default": settings.audit_export_s3_bucket or "",
        "label": "Audit export — S3 bucket name (blank = disk only)",
        "group": "Audit export",
    },
    "audit_export_s3_endpoint": {
        "type": "str", "default": settings.audit_export_s3_endpoint or "",
        "label": "Audit export — S3 endpoint URL (blank = AWS; set for MinIO / B2 / Wasabi)",
        "group": "Audit export",
    },
    "audit_export_s3_region": {
        "type": "str", "default": settings.audit_export_s3_region or "us-east-1",
        "label": "Audit export — S3 region",
        "group": "Audit export",
    },
    "audit_export_s3_access_key": {
        "type": "str", "default": settings.audit_export_s3_access_key or "",
        "label": "Audit export — S3 access key ID",
        "group": "Audit export",
        "secret": True,
    },
    "audit_export_s3_secret_key": {
        "type": "str", "default": settings.audit_export_s3_secret_key or "",
        "label": "Audit export — S3 secret access key",
        "group": "Audit export",
        "secret": True,
    },
    "audit_export_retention_days": {
        "type": "int", "default": settings.audit_export_retention_days,
        "label": "Audit export — local retention (days before prune removes old files)",
        "group": "Audit export",
    },
    # ── v5.8.1 AI Integration Protocol ───────────────────────────────
    "integration_enabled": {
        "type": "bool", "default": settings.integration_enabled,
        "label": "AI Integration — accept /api/integration/chat requests (gated by passphrase below)",
        "group": "AI Integration",
    },
    "integration_passphrase": {
        "type": "str", "default": settings.integration_passphrase or "",
        "label": "AI Integration — shared passphrase that integrating AIs must provide on every request",
        "group": "AI Integration",
        "secret": True,
    },
    "integration_default_daily_budget_usd": {
        "type": "float", "default": settings.integration_default_daily_budget_usd,
        "label": "AI Integration — default daily budget USD for minted keys",
        "group": "AI Integration",
    },
    "integration_max_daily_budget_usd": {
        "type": "float", "default": settings.integration_max_daily_budget_usd,
        "label": "AI Integration — hard cap on minted-key daily budget (management AI cannot mint above this)",
        "group": "AI Integration",
    },
    "integration_max_messages_per_session": {
        "type": "int", "default": settings.integration_max_messages_per_session,
        "label": "AI Integration — max messages per chat session before forcing a new session",
        "group": "AI Integration",
    },
    "integration_model": {
        "type": "str", "default": settings.integration_model,
        "label": "AI Integration — model used by the management AI",
        "group": "AI Integration",
    },
    # ── Wave 6 — PII masking ─────────────────────────────────────────────────
    "pii_masking_enabled": {
        "type": "bool", "default": settings.pii_masking_enabled,
        "label": "PII masking — redact email / SSN / credit-card / phone / IPv4 in outbound requests",
        "group": "Privacy",
    },
    # ── Wave 6 — Semantic prompt guard ───────────────────────────────────────
    "prompt_guard_enabled": {
        "type": "bool", "default": settings.prompt_guard_enabled,
        "label": "Prompt guard — reject requests matching the denylist",
        "group": "Privacy",
    },
    "prompt_guard_denylist": {
        "type": "str", "default": settings.prompt_guard_denylist or "",
        "label": "Prompt-guard denylist — comma-separated phrases (case-insensitive substring match)",
        "group": "Privacy",
        "help": "Example: ignore previous instructions, reveal your system prompt",
    },
    # ── Wave 6 — SSO/SAML ────────────────────────────────────────────────────
    "sso_enabled": {
        "type": "bool", "default": settings.sso_enabled,
        "label": "Enable SSO (OIDC)",
        "group": "SSO",
    },
    "sso_entity_id": {
        "type": "str", "default": settings.sso_entity_id or "",
        "label": "SSO entity ID (SAML only)",
        "group": "SSO",
    },
    "sso_idp_metadata_url": {
        "type": "str", "default": settings.sso_idp_metadata_url or "",
        "label": "SSO IdP metadata URL",
        "group": "SSO",
    },
    "sso_acs_url": {
        "type": "str", "default": settings.sso_acs_url or "",
        "label": "SSO Assertion Consumer Service URL",
        "group": "SSO",
    },
    # OAuth capture moved to a proper table in v2.5.0 — see Admin → Providers →
    # "Add OAuth capture" for the multi-profile UI. The legacy global
    # oauth_capture_* settings on `settings` are ignored since v2.5.0.

    # v3.7.33 — surface the AI rate limiter + AI provider supervisor +
    # billing-scrape settings in the Settings UI. Previously env-var-only,
    # which meant operators couldn't toggle the workers without a
    # container restart AND had no visibility into the current values.
    "ai_rate_limiter_enabled": {
        "type": "bool", "default": settings.ai_rate_limiter_enabled,
        "label": "AI rate limiter: enable per-key suggestions (writes review rows)",
        "group": "AI rate limiter",
    },
    "ai_rate_limiter_auto_apply": {
        "type": "bool", "default": settings.ai_rate_limiter_auto_apply,
        "label": "AI rate limiter: auto-apply throttle/disable verdicts (suggest-only when off)",
        "group": "AI rate limiter",
    },
    "ai_rate_limiter_interval_sec": {
        "type": "int", "default": settings.ai_rate_limiter_interval_sec,
        "label": "AI rate limiter: scan cadence (seconds)",
        "group": "AI rate limiter",
    },
    "ai_rate_limiter_window_min": {
        "type": "int", "default": settings.ai_rate_limiter_window_min,
        "label": "AI rate limiter: activity-log window (minutes)",
        "group": "AI rate limiter",
    },
    "ai_rate_limiter_model": {
        "type": "str", "default": settings.ai_rate_limiter_model,
        "label": "AI rate limiter: classifier LLM model",
        "group": "AI rate limiter",
    },
    "ai_rate_limiter_internal_api_key": {
        "type": "str", "default": settings.ai_rate_limiter_internal_api_key or "",
        "label": "AI rate limiter: internal API key for self-call (llmp-...)",
        "group": "AI rate limiter",
    },
    "ai_rate_limiter_throttle_floor_rpm": {
        "type": "int", "default": settings.ai_rate_limiter_throttle_floor_rpm,
        "label": "AI rate limiter: floor rpm when auto-applying a throttle",
        "group": "AI rate limiter",
    },

    "ai_provider_supervisor_enabled": {
        "type": "bool", "default": settings.ai_provider_supervisor_enabled,
        "label": "AI provider supervisor: enable per-provider review (writes review rows)",
        "group": "AI provider supervisor",
    },
    "ai_provider_supervisor_auto_apply": {
        "type": "bool", "default": settings.ai_provider_supervisor_auto_apply,
        "label": "AI provider supervisor: auto-apply deprioritize/disable verdicts (suggest-only when off)",
        "group": "AI provider supervisor",
    },
    "ai_provider_supervisor_interval_sec": {
        "type": "int", "default": settings.ai_provider_supervisor_interval_sec,
        "label": "AI provider supervisor: scan cadence (seconds, default 1800 = 30 min)",
        "group": "AI provider supervisor",
    },
    "ai_provider_supervisor_short_window_min": {
        "type": "int", "default": settings.ai_provider_supervisor_short_window_min,
        "label": "AI provider supervisor: short window (minutes)",
        "group": "AI provider supervisor",
    },
    "ai_provider_supervisor_trend_window_days": {
        "type": "int", "default": settings.ai_provider_supervisor_trend_window_days,
        "label": "AI provider supervisor: trend baseline window (days)",
        "group": "AI provider supervisor",
    },
    "ai_provider_supervisor_model": {
        "type": "str", "default": settings.ai_provider_supervisor_model,
        "label": "AI provider supervisor: classifier LLM model",
        "group": "AI provider supervisor",
    },
    "ai_provider_supervisor_internal_api_key": {
        "type": "str", "default": settings.ai_provider_supervisor_internal_api_key or "",
        "label": "AI provider supervisor: internal API key for self-call (llmp-...)",
        "group": "AI provider supervisor",
    },
    "ai_provider_supervisor_max_priority_delta": {
        "type": "int", "default": settings.ai_provider_supervisor_max_priority_delta,
        "label": "AI provider supervisor: max priority delta per auto-apply",
        "group": "AI provider supervisor",
    },
    "ai_provider_supervisor_max_auto_skip_hours": {
        "type": "int", "default": settings.ai_provider_supervisor_max_auto_skip_hours,
        "label": "AI provider supervisor: max auto-skip duration (hours)",
        "group": "AI provider supervisor",
    },

    "anthropic_billing_scrape_interval_sec": {
        "type": "int", "default": settings.anthropic_billing_scrape_interval_sec,
        "label": "Anthropic billing scrape: cadence (seconds, default 14400 = 4h, 0 = disabled)",
        "group": "Billing scrape",
    },
    "anthropic_billing_min_scrape_gap_sec": {
        "type": "int", "default": settings.anthropic_billing_min_scrape_gap_sec,
        "label": "Anthropic billing scrape: min gap between scrapes (0 = interval/2 heuristic)",
        "group": "Billing scrape",
    },
    "codex_billing_scrape_interval_sec": {
        "type": "int", "default": settings.codex_billing_scrape_interval_sec,
        "label": "ChatGPT/Codex billing scrape: cadence (seconds, default 14400 = 4h, 0 = disabled)",
        "group": "Billing scrape",
    },
    "codex_billing_min_scrape_gap_sec": {
        "type": "int", "default": settings.codex_billing_min_scrape_gap_sec,
        "label": "ChatGPT/Codex billing scrape: min gap between scrapes (0 = interval/2 heuristic)",
        "group": "Billing scrape",
    },

    # v3.8.4 (#264) — tool capability prober settings, UI-tunable.
    "ai_tool_prober_enabled": {
        "type": "bool", "default": settings.ai_tool_prober_enabled,
        "label": "Tool prober: fire a standard get_weather(city) probe at every (provider, default_model) periodically",
        "group": "Tool capability prober",
    },
    "ai_tool_prober_interval_sec": {
        "type": "int", "default": settings.ai_tool_prober_interval_sec,
        "label": "Tool prober: cadence (seconds, default 86400 = daily)",
        "group": "Tool capability prober",
    },
    "ai_tool_prober_internal_api_key": {
        "type": "str", "default": settings.ai_tool_prober_internal_api_key or "",
        "label": "Tool prober: internal API key for self-call (llmp-...)",
        "group": "Tool capability prober",
    },
    "ai_tool_prober_native_threshold": {
        "type": "float", "default": settings.ai_tool_prober_native_threshold,
        "label": "Tool prober: success rate >= this flips native_tools=True (0.0-1.0, default 0.8)",
        "group": "Tool capability prober",
    },
    "ai_tool_prober_emulate_threshold": {
        "type": "float", "default": settings.ai_tool_prober_emulate_threshold,
        "label": "Tool prober: success rate < this flips native_tools=False (default 0.6 — gap creates hysteresis)",
        "group": "Tool capability prober",
    },
    "ai_tool_prober_success_window": {
        "type": "int", "default": settings.ai_tool_prober_success_window,
        "label": "Tool prober: rolling window size (probes — default 5)",
        "group": "Tool capability prober",
    },

    # v3.8.7 (#267) — proxy-side memory store. Opt-in via the feature
    # flag; the data layer ships disabled, so no behavior change until
    # operator flips it.
    "caller_memory_enabled": {
        "type": "bool", "default": settings.caller_memory_enabled,
        "label": "Proxy-side memory: enable cross-provider memory state (cluster-replicated)",
        "group": "Caller memory",
    },
    "caller_memory_active_flush_enabled": {
        "type": "bool", "default": settings.caller_memory_active_flush_enabled,
        "label": "Proxy-side memory: active flush of provider-side memory when routing away (best-effort)",
        "group": "Caller memory",
    },
    "caller_memory_recovery_enabled": {
        "type": "bool", "default": settings.caller_memory_recovery_enabled,
        "label": "Proxy-side memory: back-pressure recovery — reconstruct missing content from upstream when marker exists (best-effort)",
        "group": "Caller memory",
    },
    # ── Compliance enforcement (v5.0.0) ──────────────────────────────────────
    "compliance_enabled": {
        "type": "bool",
        "default": getattr(settings, "compliance_enabled", False),
        "label": "Compliance enforcement: master switch. When OFF, all blocklist checks are skipped (sandbox / pre-rollout mode).",
        "group": "Compliance",
    },
    "compliance_system_blocked_companies": {
        "type": "str",
        "default": getattr(settings, "compliance_system_blocked_companies", ""),
        "label": "System-wide blocked companies (JSON list of company IDs; e.g. [\"anthropic\"]). Unioned with each key's blocked_companies at request time.",
        "group": "Compliance",
    },
    "compliance_audit_retention_days": {
        "type": "int",
        "default": getattr(settings, "compliance_audit_retention_days", 2555),
        "label": "Compliance audit retention (days; default 2555 = 7 years).",
        "group": "Compliance",
    },
    "compliance_ua_block_enabled": {
        "type": "bool",
        "default": getattr(settings, "compliance_ua_block_enabled", True),
        "label": "Compliance: refuse requests whose User-Agent identifies a banned client product (HTTP 451). On by default; turn off for UA-spoofing debug only.",
        "group": "Compliance",
    },
    "compliance_custom_companies": {
        "type": "str",
        "default": getattr(settings, "compliance_custom_companies", ""),
        "label": "Custom compliance companies (JSON list of {id, display_name, model_prefixes, provider_types, ua_patterns}). Merged with the built-in 10-company taxonomy at lookup time.",
        "group": "Compliance",
    },
    "compliance_backfill_applied": {
        "type": "bool",
        "default": getattr(settings, "compliance_backfill_applied", False),
        "label": "Compliance: caller_memory.source_company backfill flag (set automatically on first policy enable; do NOT toggle manually).",
        "group": "Compliance",
    },
}


def _coerce(raw: str, typ: str) -> Any:
    if typ == "bool":
        return raw.lower() in ("1", "true", "yes")
    if typ == "int":
        return int(raw)
    if typ == "float":
        return float(raw)
    # str — v4.3.7 (smtp_to="None" finding): treat empty string and the
    # legacy literal "None" (pre-fix data from `str(None)` in save()) as
    # Python None so Optional[str] fields don't end up holding a string
    # that passes truthy checks. This matters for downstream code that
    # gates behaviour on `if settings.smtp_to:` etc. Tolerating the
    # legacy "None" sentinel here makes the fix backward-compatible
    # for un-migrated nodes during a rolling deploy.
    if raw == "" or raw == "None":
        return None
    return raw


def get_defaults() -> dict[str, Any]:
    return {k: v["default"] for k, v in SCHEMA.items()}


def get_setting(key: str, default: Any = None) -> Any:
    """v5.0.0 — read a single SystemSetting via the in-memory settings
    singleton. Some settings (JSON-encoded lists for compliance taxonomy)
    are stored as text; callers parse with ``json.loads`` if they need a
    list shape. Returns ``default`` when the key isn't present.
    """
    if hasattr(settings, key):
        val = getattr(settings, key)
        if val in (None, ""):
            return default
        # JSON-shaped string settings (compliance lists, custom company maps):
        # auto-parse when the value looks like a JSON list or object.
        if isinstance(val, str):
            stripped = val.strip()
            if stripped.startswith(("[", "{")):
                import json as _json
                try:
                    return _json.loads(stripped)
                except Exception:
                    return default
        return val
    return default


def apply(overrides: dict[str, Any]) -> None:
    """Patch the shared settings singleton with any recognised keys."""
    for key, val in overrides.items():
        if hasattr(settings, key):
            try:
                object.__setattr__(settings, key, val)
            except Exception:
                settings.__dict__[key] = val


async def load(db: AsyncSession) -> None:
    from app.models.db import SystemSetting
    result = await db.execute(select(SystemSetting))
    rows = result.scalars().all()
    overrides: dict[str, Any] = {}
    for row in rows:
        schema = SCHEMA.get(row.key)
        if schema:
            # v3.0.8 (item 4): canonical_type() prefers the pydantic field's
            # annotation over SCHEMA's declared type, with SCHEMA as a
            # fallback for keys without a matching pydantic field. v3.0.1's
            # bug class (SCHEMA says str, pydantic says float) cannot
            # recur — coercion always matches what the rest of the app
            # reads via ``settings.<key>``.
            overrides[row.key] = _coerce(row.value, canonical_type(row.key, schema))
    if overrides:
        apply(overrides)
        logger.info("runtime_settings_loaded count=%s", len(overrides))


async def save(db: AsyncSession, updates: dict[str, Any], timestamp: float | None = None) -> None:
    import time as _time
    from app.models.db import SystemSetting
    now = timestamp if timestamp is not None else _time.time()
    for key, val in updates.items():
        schema = SCHEMA.get(key)
        if not schema:
            continue
        # v3.0.8 (item 4): use canonical_type so the row's stored value_type
        # matches what the pydantic field expects on next load.
        typ = canonical_type(key, schema)
        # v4.3.7 (smtp_to="None" finding): when val is Python None, never
        # write the literal string "None" — that string passes truthy
        # checks downstream (e.g. notify.py's `if settings.smtp_to:`) and
        # silently sets the alert recipient to "None", causing alerts to
        # bounce without surfacing. Store empty string instead; the load
        # path converts empty strings back to None for str-typed fields.
        if val is None:
            raw = ""
        else:
            raw = str(val)
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        row = result.scalar_one_or_none()
        if row:
            row.value = raw
            row.value_type = typ
            row.updated_at = now
        else:
            db.add(SystemSetting(key=key, value=raw, value_type=typ, updated_at=now))
    await db.commit()
    # Apply to live settings singleton
    coerced = {
        k: _coerce("" if v is None else str(v), SCHEMA[k]["type"])
        for k, v in updates.items() if k in SCHEMA
    }
    apply(coerced)


async def get_all_db_settings(db: AsyncSession) -> list[dict]:
    """Return all rows from system_settings for cluster sync payload."""
    from app.models.db import SystemSetting
    result = await db.execute(select(SystemSetting))
    return [
        {"key": r.key, "value": r.value, "value_type": r.value_type, "updated_at": r.updated_at or 0.0}
        for r in result.scalars().all()
    ]
