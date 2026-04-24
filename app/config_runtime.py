"""
Runtime settings — DB-backed overrides on top of env-var defaults.

Keys, types, and human-readable labels for every tunable setting.
`load(db)` is called on startup and after each PUT /api/settings.
`apply(overrides)` patches the shared `settings` singleton in-place so
all existing code that reads `from app.config import settings` picks up
the change without modification.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings

logger = logging.getLogger(__name__)


SCHEMA: dict[str, dict] = {
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
    "cot_verify_step_timeout_sec": {"type": "str", "default": str(settings.cot_verify_step_timeout_sec), "label": "Per-step verify execution timeout (seconds)"},
    "cot_plan_compact": {"type": "bool", "default": settings.cot_plan_compact, "label": "Chain-of-Draft plan: ~5-word mini-steps (-78% plan tokens, faster TTFT)"},
    "fallback_enabled": {"type": "bool", "default": settings.fallback_enabled, "label": "Ordered fallback: on provider failure, try next-ranked candidate"},
    "fallback_max_providers": {"type": "int", "default": settings.fallback_max_providers, "label": "Max providers to try per request before giving up"},
    "task_auto_detect_enabled": {"type": "bool", "default": settings.task_auto_detect_enabled, "label": "Auto-classify LMRH task= hint via embedding cosine (~40ms overhead)"},
    "shadow_traffic_rate": {"type": "str", "default": str(settings.shadow_traffic_rate), "label": "Shadow-traffic fraction (0.0–1.0); 0.01 = mirror 1% of requests"},
    "shadow_candidate_provider_id": {"type": "str", "default": settings.shadow_candidate_provider_id, "label": "Provider ID to shadow-test"},
    "structured_output_enabled": {"type": "bool", "default": settings.structured_output_enabled, "label": "Enforce JSON-Schema response_format via repair loop"},
    "structured_output_max_repairs": {"type": "int", "default": settings.structured_output_max_repairs, "label": "Max structured-output repair attempts (default 2)"},
    "vision_route_enabled": {"type": "bool", "default": settings.vision_route_enabled, "label": "Vision-to-text: route images through VLM instead of stripping"},
    # Semantic cache (Wave 1 #3)
    "semantic_cache_enabled":          {"type": "bool", "default": settings.semantic_cache_enabled,         "label": "Enable semantic cache globally"},
    "semantic_cache_threshold":        {"type": "str",  "default": str(settings.semantic_cache_threshold),  "label": "Cosine threshold (0.0–1.0)"},
    "semantic_cache_ttl_sec":          {"type": "int",  "default": settings.semantic_cache_ttl_sec,         "label": "TTL (seconds)"},
    "semantic_cache_min_response_chars":{"type":"int",  "default": settings.semantic_cache_min_response_chars,"label":"Min response chars to cache"},
    # Hedged requests (Wave 1 #4)
    "hedge_enabled":     {"type": "bool", "default": settings.hedge_enabled,     "label": "Enable hedged requests globally"},
    "hedge_max_per_sec": {"type": "str",  "default": str(settings.hedge_max_per_sec), "label": "Max hedge requests per second (global bucket)"},
    # Native reasoning
    "native_thinking_budget_tokens": {"type": "int", "default": settings.native_thinking_budget_tokens, "label": "Thinking budget tokens (Gemini 2.5 / Anthropic)"},
    "native_reasoning_effort":       {"type": "str", "default": settings.native_reasoning_effort,       "label": "Reasoning effort (o-series: low / medium / high)"},
    # Circuit breaker
    "circuit_breaker_threshold":    {"type": "int", "default": settings.circuit_breaker_threshold,    "label": "CB failure threshold"},
    "circuit_breaker_timeout_sec":  {"type": "int", "default": settings.circuit_breaker_timeout_sec,  "label": "CB timeout (seconds)"},
    "circuit_breaker_halfopen_sec": {"type": "int", "default": settings.circuit_breaker_halfopen_sec, "label": "CB half-open window (seconds)"},
    "circuit_breaker_success_needed":{"type":"int", "default": settings.circuit_breaker_success_needed,"label": "CB successes needed to close"},
    "hold_down_sec":                {"type": "int", "default": settings.hold_down_sec,                "label": "Provider hold-down (seconds)"},
    # SMTP
    "smtp_enabled": {"type": "bool",  "default": settings.smtp_enabled, "label": "Enable email alerts"},
    "smtp_host":    {"type": "str",   "default": settings.smtp_host or "",    "label": "SMTP host"},
    "smtp_port":    {"type": "int",   "default": settings.smtp_port,         "label": "SMTP port"},
    "smtp_from":    {"type": "str",   "default": settings.smtp_from or "",   "label": "From address"},
    "smtp_to":      {"type": "str",   "default": settings.smtp_to or "",     "label": "Alert recipient"},
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
    # ── OAuth capture (research tool for Claude Pro Max provider) ────────────
    "oauth_capture_enabled": {
        "type": "bool", "default": settings.oauth_capture_enabled,
        "label": "OAuth capture recorder — enable /api/oauth-capture passthrough",
        "group": "OAuth capture",
        "help": (
            "Research tool for reverse-engineering the claude-code CLI's OAuth flow. "
            "When enabled, /api/oauth-capture/{path} records requests and forwards "
            "them to the configured upstream. See docs/claude-pro-max-oauth-capture.md."
        ),
    },
    "oauth_capture_upstream": {
        "type": "str", "default": settings.oauth_capture_upstream or "https://console.anthropic.com",
        "label": "OAuth capture upstream URL (no trailing slash)",
        "group": "OAuth capture",
    },
    "oauth_capture_secret": {
        "type": "str", "default": settings.oauth_capture_secret or "",
        "label": "OAuth capture shared secret (required via ?cap=... or X-Capture-Secret header)",
        "group": "OAuth capture",
        "secret": True,
        "help": "Leave blank to disable the secret check (NOT recommended on public proxies).",
    },
}


def _coerce(raw: str, typ: str) -> Any:
    if typ == "bool":
        return raw.lower() in ("1", "true", "yes")
    if typ == "int":
        return int(raw)
    if typ == "float":
        return float(raw)
    return raw


def get_defaults() -> dict[str, Any]:
    return {k: v["default"] for k, v in SCHEMA.items()}


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
            overrides[row.key] = _coerce(row.value, row.value_type)
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
        typ = schema["type"]
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
    coerced = {k: _coerce(str(v), SCHEMA[k]["type"]) for k, v in updates.items() if k in SCHEMA}
    apply(coerced)


async def get_all_db_settings(db: AsyncSession) -> list[dict]:
    """Return all rows from system_settings for cluster sync payload."""
    from app.models.db import SystemSetting
    result = await db.execute(select(SystemSetting))
    return [
        {"key": r.key, "value": r.value, "value_type": r.value_type, "updated_at": r.updated_at or 0.0}
        for r in result.scalars().all()
    ]
