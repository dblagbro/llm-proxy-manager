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
    # Hedged requests (Wave 1 #4)
    "hedge_enabled":     {"type": "bool", "default": settings.hedge_enabled,     "label": "Enable hedged requests globally"},
    "hedge_max_per_sec": {"type": "float",  "default": settings.hedge_max_per_sec, "label": "Max hedge requests per second (global bucket)"},
    # Native reasoning
    "native_thinking_budget_tokens": {"type": "int", "default": settings.native_thinking_budget_tokens, "label": "Thinking budget tokens (Gemini 2.5 / Anthropic)"},
    "native_reasoning_effort":       {"type": "str", "default": settings.native_reasoning_effort,       "label": "Reasoning effort (o-series: low / medium / high)"},
    # Circuit breaker
    # v2.8.4 — activity-log payload capture
    "activity_log_capture_bodies": {
        "type": "bool", "default": settings.activity_log_capture_bodies,
        "label": "Activity log: capture full request + response payloads (text + tool calls). Adds DB rows ~5-100KB each.",
        "group": "Activity log",
    },
    "activity_log_max_body_chars": {
        "type": "int", "default": settings.activity_log_max_body_chars,
        "label": "Activity log: max characters to keep per body (truncated with ellipsis past this).",
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
    # OAuth capture moved to a proper table in v2.5.0 — see Admin → Providers →
    # "Add OAuth capture" for the multi-profile UI. The legacy global
    # oauth_capture_* settings on `settings` are ignored since v2.5.0.
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
