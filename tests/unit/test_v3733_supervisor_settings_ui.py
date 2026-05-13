"""v3.7.33 — surface AI rate limiter + AI provider supervisor + billing-scrape
settings in the runtime SCHEMA so they're configurable via the Settings UI.

Operator caught 2026-05-13 that the supervisor settings shipped in v3.7.30/.31
were env-var-only — required a container restart to change, and operators had
no visibility into current values. This ship adds them to ``config_runtime.SCHEMA``
which auto-surfaces them in the Settings UI (and makes them hot-reloadable).

Bonus: same fix applied to the parallel v3.7.10 AI rate limiter + v3.7.0/.24/.27
billing-scrape settings that had the same env-only gap.
"""
from __future__ import annotations

from pathlib import Path


# ── AI provider supervisor — operator's specific concern ──────────


def test_all_9_supervisor_settings_in_schema():
    """The operator must be able to toggle all 9 supervisor knobs from
    the Settings UI without a redeploy."""
    from app.config_runtime import SCHEMA
    expected = {
        "ai_provider_supervisor_enabled",
        "ai_provider_supervisor_auto_apply",
        "ai_provider_supervisor_interval_sec",
        "ai_provider_supervisor_short_window_min",
        "ai_provider_supervisor_trend_window_days",
        "ai_provider_supervisor_model",
        "ai_provider_supervisor_internal_api_key",
        "ai_provider_supervisor_max_priority_delta",
        "ai_provider_supervisor_max_auto_skip_hours",
    }
    missing = expected - set(SCHEMA.keys())
    assert not missing, f"missing supervisor settings in SCHEMA: {missing}"


def test_supervisor_settings_grouped_under_label():
    """All 9 supervisor settings carry the same group label so the UI
    can render them as a cohesive section."""
    from app.config_runtime import SCHEMA
    sup_keys = [k for k in SCHEMA if k.startswith("ai_provider_supervisor_")]
    groups = {SCHEMA[k].get("group") for k in sup_keys}
    assert groups == {"AI provider supervisor"}


def test_supervisor_settings_have_human_labels():
    """Operator-facing labels must be non-empty (don't render the raw
    pydantic field name)."""
    from app.config_runtime import SCHEMA
    sup_keys = [k for k in SCHEMA if k.startswith("ai_provider_supervisor_")]
    for k in sup_keys:
        label = SCHEMA[k].get("label", "")
        assert label and len(label) > 5, f"{k} has weak label: {label!r}"


def test_supervisor_settings_correct_types():
    """Each setting's SCHEMA type must match the pydantic field type,
    or config_runtime's boot-time consistency check will WARN."""
    from app.config_runtime import SCHEMA
    bools = ("ai_provider_supervisor_enabled", "ai_provider_supervisor_auto_apply")
    ints = ("ai_provider_supervisor_interval_sec", "ai_provider_supervisor_short_window_min",
            "ai_provider_supervisor_trend_window_days",
            "ai_provider_supervisor_max_priority_delta",
            "ai_provider_supervisor_max_auto_skip_hours")
    strs = ("ai_provider_supervisor_model", "ai_provider_supervisor_internal_api_key")
    for k in bools:
        assert SCHEMA[k]["type"] == "bool"
    for k in ints:
        assert SCHEMA[k]["type"] == "int"
    for k in strs:
        assert SCHEMA[k]["type"] == "str"


# ── Parallel gaps fixed proactively ───────────────────────────────


def test_rate_limiter_settings_in_schema():
    """v3.7.10 AI rate limiter had the same env-only gap as the
    supervisor; same fix applied."""
    from app.config_runtime import SCHEMA
    expected = {
        "ai_rate_limiter_enabled",
        "ai_rate_limiter_auto_apply",
        "ai_rate_limiter_interval_sec",
        "ai_rate_limiter_window_min",
        "ai_rate_limiter_model",
        "ai_rate_limiter_internal_api_key",
        "ai_rate_limiter_throttle_floor_rpm",
    }
    missing = expected - set(SCHEMA.keys())
    assert not missing, f"missing rate-limiter settings in SCHEMA: {missing}"


def test_billing_scrape_settings_in_schema():
    """v3.7.0/.24/.27 billing scrape cadence + min-gap were env-only too."""
    from app.config_runtime import SCHEMA
    expected = {
        "anthropic_billing_scrape_interval_sec",
        "anthropic_billing_min_scrape_gap_sec",
        "codex_billing_scrape_interval_sec",
        "codex_billing_min_scrape_gap_sec",
    }
    missing = expected - set(SCHEMA.keys())
    assert not missing, f"missing billing-scrape settings in SCHEMA: {missing}"


# ── Settings API picks them up automatically ──────────────────────


def test_settings_api_iterates_schema():
    """The PUT /api/settings handler rejects unknown keys via SCHEMA
    membership — adding to SCHEMA is sufficient to make a key
    operator-settable, no additional wiring."""
    src = Path("app/api/settings_api.py").read_text()
    assert "config_runtime.SCHEMA.items()" in src
    assert "k not in config_runtime.SCHEMA" in src


def test_runtime_apply_picks_up_new_keys():
    """config_runtime.apply() patches the settings singleton in-place.
    Verify it can handle the new keys without raising."""
    from app import config_runtime
    overrides = {
        "ai_provider_supervisor_enabled": True,
        "ai_provider_supervisor_interval_sec": 600,
        "ai_provider_supervisor_model": "claude-haiku-4-5-20251001",
    }
    config_runtime.apply(overrides)
    # Verify the in-memory settings reflect the patch
    from app.config import settings
    assert settings.ai_provider_supervisor_enabled is True
    assert settings.ai_provider_supervisor_interval_sec == 600
    # Reset to safe defaults so other tests aren't affected
    config_runtime.apply({
        "ai_provider_supervisor_enabled": False,
        "ai_provider_supervisor_interval_sec": 1800,
    })


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 33)
