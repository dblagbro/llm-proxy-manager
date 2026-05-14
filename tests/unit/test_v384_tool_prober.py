"""v3.8.4 (#264) — periodic tool-call probe + auto-native_tools.

Verifies the prober's behavior:
- Probe a standard get_weather(city) tool-call request at every
  (provider, default_model)
- Score the response (called / parseable / correct_args)
- Persist to model_tool_probe table
- Apply hysteresis to ModelCapability.native_tools based on rolling
  window success rate
- Surface admin endpoints for manual trigger + history
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Schema ─────────────────────────────────────────────────────────


def test_model_tool_probe_table_exists():
    from app.models.db import ModelToolProbe
    cols = {c.name for c in ModelToolProbe.__table__.columns}
    expected = {
        "id", "provider_id", "model_id", "captured_at",
        "called", "parseable", "correct_args",
        "error", "raw_excerpt", "response_format",
    }
    missing = expected - cols
    assert not missing, f"ModelToolProbe missing columns: {missing}"


def test_model_tool_probe_indexed_for_rolling_query():
    """Rolling-window evaluator queries by (provider_id, model_id)
    ordered by captured_at DESC. provider_id + captured_at must be
    indexed."""
    from app.models.db import ModelToolProbe
    cols = {c.name: c for c in ModelToolProbe.__table__.columns}
    assert cols["provider_id"].index is True
    assert cols["captured_at"].index is True
    assert cols["model_id"].index is True


# ── Settings ──────────────────────────────────────────────────────


def test_prober_settings_present():
    from app.config import settings
    for key in (
        "ai_tool_prober_enabled",
        "ai_tool_prober_interval_sec",
        "ai_tool_prober_internal_api_key",
        "ai_tool_prober_native_threshold",
        "ai_tool_prober_emulate_threshold",
        "ai_tool_prober_success_window",
    ):
        assert hasattr(settings, key), f"missing {key}"
    assert settings.ai_tool_prober_enabled is False  # default OFF
    assert settings.ai_tool_prober_interval_sec == 86400  # daily
    assert settings.ai_tool_prober_native_threshold == 0.8
    assert settings.ai_tool_prober_emulate_threshold == 0.6
    # Hysteresis gap must be non-zero so the flag doesn't flap on
    # borderline 0.7 success rates
    assert settings.ai_tool_prober_native_threshold > settings.ai_tool_prober_emulate_threshold


def test_prober_settings_exposed_in_ui_schema():
    from app.config_runtime import SCHEMA
    keys = [k for k in SCHEMA if k.startswith("ai_tool_prober_")]
    assert len(keys) >= 6
    groups = {SCHEMA[k].get("group") for k in keys}
    assert groups == {"Tool capability prober"}


# ── Worker module ─────────────────────────────────────────────────


def test_prober_module_exists():
    import importlib
    mod = importlib.import_module("app.monitoring.tool_capability_prober")
    for fn in (
        "start", "_scan_loop", "_probe_loop_once",
        "probe_one_model", "evaluate_probe_response",
        "update_native_tools_from_rolling",
    ):
        assert hasattr(mod, fn), f"missing {fn}"


def test_prober_wired_into_main_lifespan():
    src = Path("app/main.py").read_text()
    assert "tool_capability_prober" in src
    assert "_ai_tool_p.start()" in src


def test_prober_uses_recursion_guard_header():
    src = Path("app/monitoring/tool_capability_prober.py").read_text()
    assert '"X-Internal-Source": "ai_tool_prober"' in src


def test_prober_pins_routing_via_llm_hint():
    """Probe MUST pin routing to the target provider — otherwise the
    router could pick a different provider for the request, and the
    'probe of provider X' actually tests provider Y."""
    src = Path("app/monitoring/tool_capability_prober.py").read_text()
    assert "provider-hint=" in src
    assert ";require" in src


# ── evaluate_probe_response ───────────────────────────────────────


def test_evaluate_recognizes_correct_response():
    from app.monitoring.tool_capability_prober import evaluate_probe_response
    body = {
        "content": [
            {"type": "tool_use", "name": "get_weather", "input": {"city": "San Francisco"}},
        ],
    }
    called, parseable, correct_args = evaluate_probe_response(body)
    assert called is True
    assert parseable is True
    assert correct_args is True


def test_evaluate_wrong_tool_name():
    from app.monitoring.tool_capability_prober import evaluate_probe_response
    body = {
        "content": [
            {"type": "tool_use", "name": "weather_lookup", "input": {"city": "SF"}},
        ],
    }
    called, parseable, correct_args = evaluate_probe_response(body)
    assert called is True
    assert parseable is False
    assert correct_args is False


def test_evaluate_missing_city_arg():
    from app.monitoring.tool_capability_prober import evaluate_probe_response
    body = {
        "content": [
            {"type": "tool_use", "name": "get_weather", "input": {"location": "SF"}},
        ],
    }
    called, parseable, correct_args = evaluate_probe_response(body)
    assert called is True
    assert parseable is True
    assert correct_args is False  # wrong key


def test_evaluate_no_tool_call():
    from app.monitoring.tool_capability_prober import evaluate_probe_response
    body = {"content": [{"type": "text", "text": "Sunny and 72°F."}]}
    called, parseable, correct_args = evaluate_probe_response(body)
    assert called is False
    assert parseable is False
    assert correct_args is False


def test_evaluate_handles_garbage_input():
    from app.monitoring.tool_capability_prober import evaluate_probe_response
    assert evaluate_probe_response(None) == (False, False, False)
    assert evaluate_probe_response("not a dict") == (False, False, False)
    assert evaluate_probe_response({}) == (False, False, False)


# ── Admin endpoints ────────────────────────────────────────────────


def test_prober_admin_endpoints_registered():
    from app.api.tool_prober import router
    paths = {r.path for r in router.routes}
    assert "/api/providers/{provider_id}/tool-prober-trigger" in paths
    assert "/api/providers/{provider_id}/tool-probe-history" in paths


def test_prober_router_included_in_app():
    src = Path("app/main.py").read_text()
    assert "from app.api.tool_prober import router as tool_prober_router" in src
    assert "app.include_router(tool_prober_router)" in src


def test_trigger_respects_manual_override():
    """Manual probe trigger refuses when provider is under manual
    override — operator must release the lock to probe a locked
    provider (consistent with the v3.7.32 supervisor endpoints)."""
    src = Path("app/api/tool_prober.py").read_text()
    idx = src.index("async def trigger_probe_now")
    body = src[idx:idx + 2000]
    assert "manual_override_until" in body
    assert "409" in body


# ── Hysteresis logic ───────────────────────────────────────────────


def test_hysteresis_gap_documented_in_source():
    """The 0.6 / 0.8 thresholds create a 'no-change' band of 0.6-0.8
    so borderline models don't flap. Source-level check that the gap
    is intentional."""
    src = Path("app/monitoring/tool_capability_prober.py").read_text()
    # Both threshold helpers + the logic that uses them
    assert "_native_threshold" in src
    assert "_emulate_threshold" in src
    idx = src.index("def update_native_tools_from_rolling")
    body = src[idx:idx + 3000]
    # Must check rate >= native first, then rate < emulate
    assert "rate >= native_thr" in body
    assert "rate < emul_thr" in body


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 8, 4)
