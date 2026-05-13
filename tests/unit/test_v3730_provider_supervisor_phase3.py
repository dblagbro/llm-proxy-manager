"""v3.7.30 (#252 phase 3) — ProviderAiReview table + stats compute helper.

Phase 3 adds:
- ``ProviderAiReview`` SQLAlchemy model (table auto-created via
  Base.metadata.create_all, no ALTER TABLE needed for new tables)
- ``compute_provider_stats`` helper that aggregates short/long-window
  signals from activity_log + TTFT from hedging.py
- Settings for the upcoming supervisor worker (Phase 4)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── ProviderAiReview table ─────────────────────────────────────────


def test_provider_ai_review_model_exists():
    from app.models.db import ProviderAiReview
    cols = {c.name for c in ProviderAiReview.__table__.columns}
    expected = {
        "id", "provider_id", "captured_at",
        "llm_model", "llm_verdict", "llm_reasoning",
        "suggested_priority_delta", "suggested_auto_skip_hours",
        "stats_summary",
        "applied_at", "applied_action", "prior_priority", "prior_auto_skip_until",
        "reverted_at", "dismissed_at",
    }
    missing = expected - cols
    assert not missing, f"ProviderAiReview missing columns: {missing}"


def test_provider_ai_review_table_name():
    from app.models.db import ProviderAiReview
    assert ProviderAiReview.__tablename__ == "provider_ai_review"


def test_provider_ai_review_indexed_columns():
    """provider_id + captured_at must be indexed — supervisor reads
    'latest review per provider' on every cycle."""
    from app.models.db import ProviderAiReview
    cols = {c.name: c for c in ProviderAiReview.__table__.columns}
    assert cols["provider_id"].index is True
    assert cols["captured_at"].index is True


# ── Settings ───────────────────────────────────────────────────────


def test_config_has_supervisor_settings():
    from app.config import settings
    for key in (
        "ai_provider_supervisor_enabled",
        "ai_provider_supervisor_auto_apply",
        "ai_provider_supervisor_interval_sec",
        "ai_provider_supervisor_short_window_min",
        "ai_provider_supervisor_trend_window_days",
        "ai_provider_supervisor_model",
        "ai_provider_supervisor_internal_api_key",
        "ai_provider_supervisor_max_priority_delta",
        "ai_provider_supervisor_max_auto_skip_hours",
    ):
        assert hasattr(settings, key), f"missing setting {key}"


def test_supervisor_defaults_safe():
    """Default OFF — Phase 4 ships behind opt-in flag so a deploy
    doesn't accidentally start firing LLM calls."""
    from app.config import settings
    assert settings.ai_provider_supervisor_enabled is False
    assert settings.ai_provider_supervisor_auto_apply is False


def test_supervisor_locked_decisions():
    """2026-05-13 operator-locked: model = Haiku 4.5, trend window = 1d."""
    from app.config import settings
    assert settings.ai_provider_supervisor_model == "claude-haiku-4-5-20251001"
    assert settings.ai_provider_supervisor_trend_window_days == 1


def test_supervisor_interval_30min_default():
    from app.config import settings
    assert settings.ai_provider_supervisor_interval_sec == 1800


# ── Stats compute helper ───────────────────────────────────────────


def test_stats_helper_module_exists():
    import importlib
    mod = importlib.import_module("app.monitoring.ai_provider_supervisor_stats")
    assert hasattr(mod, "compute_provider_stats")


@pytest.mark.asyncio
async def test_compute_provider_stats_returns_required_keys():
    """Output dict has the structure the worker (Phase 4) will pass
    to the LLM."""
    from app.monitoring.ai_provider_supervisor_stats import compute_provider_stats
    fake_result = MagicMock()
    fake_result.all = MagicMock(return_value=[])
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=fake_result)
    stats = await compute_provider_stats(fake_db, "p-test")
    for key in ("provider_id", "short_window", "long_window", "trend", "ttft", "captured_at"):
        assert key in stats, f"missing top-level key {key}"
    assert stats["provider_id"] == "p-test"
    assert stats["short_window"]["window_minutes"] == 30
    assert stats["long_window"]["window_days"] == 1


@pytest.mark.asyncio
async def test_compute_provider_stats_aggregates_request_counts():
    from app.monitoring.ai_provider_supervisor_stats import compute_provider_stats
    fake_rows = [
        ("info", '{"in_tok": 100, "out_tok": 50, "cost_usd": 0.001, "latency_ms": 250}'),
        ("info", '{"in_tok": 200, "out_tok": 75, "cost_usd": 0.002, "latency_ms": 300}'),
        ("warning", '{"in_tok": 50, "out_tok": 0, "cost_usd": 0, "error_class": "rate_limit"}'),
    ]
    fake_result = MagicMock()
    fake_result.all = MagicMock(return_value=fake_rows)
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=fake_result)
    stats = await compute_provider_stats(fake_db, "p-test")
    short = stats["short_window"]
    assert short["requests"] == 3
    assert short["warnings"] == 1
    assert short["errors"] == 0
    assert short["in_tok_total"] == 350
    assert short["out_tok_total"] == 125
    # success rate = 2/3 = 66.7%
    assert short["success_rate_pct"] == round(100.0 * 2 / 3, 1)
    assert short["error_class_breakdown"] == {"rate_limit": 1}


@pytest.mark.asyncio
async def test_compute_provider_stats_skips_trend_when_long_window_sparse():
    """If long-window has <5 requests, trend deltas are undefined —
    helper omits them rather than computing nonsense from tiny n."""
    from app.monitoring.ai_provider_supervisor_stats import compute_provider_stats
    fake_rows = [
        ("info", '{"in_tok": 100, "out_tok": 50, "cost_usd": 0.001}'),
        ("info", '{"in_tok": 200, "out_tok": 75, "cost_usd": 0.002}'),
    ]
    fake_result = MagicMock()
    fake_result.all = MagicMock(return_value=fake_rows)
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=fake_result)
    stats = await compute_provider_stats(fake_db, "p-test")
    # 2 requests in long window (<5 threshold) → no trend deltas
    assert stats["trend"] == {}


@pytest.mark.asyncio
async def test_compute_provider_stats_includes_ttft_when_hedging_has_data():
    from app.monitoring.ai_provider_supervisor_stats import compute_provider_stats
    fake_result = MagicMock()
    fake_result.all = MagicMock(return_value=[])
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=fake_result)
    with patch("app.routing.hedging.peak_ewma", return_value=123.4):
        with patch("app.routing.hedging.provider_p95_ms", return_value=456.7):
            stats = await compute_provider_stats(fake_db, "p-test")
    assert stats["ttft"]["peak_ewma_ms"] == 123.4
    assert stats["ttft"]["p95_ms"] == 456.7


@pytest.mark.asyncio
async def test_compute_provider_stats_handles_missing_event_meta():
    """Activity log rows may have null/malformed event_meta — helper
    must not crash."""
    from app.monitoring.ai_provider_supervisor_stats import compute_provider_stats
    fake_rows = [
        ("info", None),
        ("info", '{"in_tok": null}'),
        ("info", "not even json"),
        ("info", '{"in_tok": 100, "out_tok": 50}'),
    ]
    fake_result = MagicMock()
    fake_result.all = MagicMock(return_value=fake_rows)
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=fake_result)
    stats = await compute_provider_stats(fake_db, "p-test")
    assert stats["short_window"]["requests"] == 4
    # Only the last row had non-null tokens; helper coerced others to 0
    assert stats["short_window"]["in_tok_total"] == 100
    assert stats["short_window"]["out_tok_total"] == 50


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 30)
