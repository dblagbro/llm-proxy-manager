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
