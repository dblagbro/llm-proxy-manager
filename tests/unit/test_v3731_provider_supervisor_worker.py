"""v3.7.31 (#252 phase 4) — AI provider supervisor worker.

Phase 4 adds the actual supervisor that:
- Runs on a configurable cadence (default 30 min, feature-flag gated)
- Computes per-provider stats via Phase 3 helper
- Calls the proxy's own /v1/messages with X-Internal-Source header
  for classification
- Writes ProviderAiReview row
- Auto-applies verdict when ai_provider_supervisor_auto_apply=True
  AND provider isn't manually overridden
- Cluster syncs reviews via existing BUG-016 pattern
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Worker module structure ────────────────────────────────────────


def test_worker_module_exists():
    import importlib
    mod = importlib.import_module("app.monitoring.ai_provider_supervisor")
    for fn in (
        "start", "_scan_loop", "_scan_all_once",
        "review_one_provider", "classify_with_llm",
        "build_prompt", "parse_llm_response",
        "_apply_suggestion",
    ):
        assert hasattr(mod, fn), f"missing {fn} in ai_provider_supervisor"


def test_worker_started_in_main_lifespan():
    src = Path("app/main.py").read_text()
    assert "ai_provider_supervisor" in src
    assert "_ai_sup.start()" in src


def test_worker_uses_recursion_guard_header():
    """Every LLM call must carry X-Internal-Source: ai_provider_supervisor
    so the call's own activity-log row is filterable (mirrors v3.7.10
    pattern)."""
    src = Path("app/monitoring/ai_provider_supervisor.py").read_text()
    assert '"X-Internal-Source": "ai_provider_supervisor"' in src


def test_worker_calls_localhost_proxy():
    """Self-call via http://localhost:3000/v1/messages so the call
    routes through our own logic and lands in activity_log."""
    src = Path("app/monitoring/ai_provider_supervisor.py").read_text()
    assert "http://localhost:3000/v1/messages" in src


def test_worker_no_op_when_disabled():
    """When ai_provider_supervisor_enabled=False, the loop sleeps but
    doesn't fire reviews. Default OFF."""
    src = Path("app/monitoring/ai_provider_supervisor.py").read_text()
    assert "if not _enabled():" in src


# ── Manual override respect ────────────────────────────────────────


def test_review_one_provider_skips_when_manual_override_set():
    src = Path("app/monitoring/ai_provider_supervisor.py").read_text()
    idx = src.index("async def review_one_provider")
    body = src[idx:idx + 3000]
    assert "manual_override_until" in body
    assert "skip_manual_override" in body


def test_apply_suggestion_double_checks_manual_override():
    """Defensive re-check inside _apply_suggestion catches a race
    where the lock was set between stats compute and apply."""
    src = Path("app/monitoring/ai_provider_supervisor.py").read_text()
    idx = src.index("async def _apply_suggestion")
    body = src[idx:idx + 2000]
    assert "manual_override_until" in body
    assert "apply_skipped_manual_override" in body


# ── Prompt + parse ─────────────────────────────────────────────────


def test_build_prompt_includes_provider_context():
    from app.monitoring.ai_provider_supervisor import build_prompt
    p = build_prompt("test-provider", "claude-oauth", {"short_window": {"requests": 100}})
    assert "test-provider" in p
    assert "claude-oauth" in p
    # JSON schema instruction must be present
    assert '"verdict"' in p
    assert "normal" in p
    assert "deprioritize" in p
    assert "disable" in p
    assert "investigate" in p


def test_parse_llm_response_validates_verdict():
    from app.monitoring.ai_provider_supervisor import parse_llm_response
    assert parse_llm_response('{"verdict": "normal", "reasoning": "ok"}') == {
        "verdict": "normal",
        "reasoning": "ok",
        "suggested_priority_delta": None,
        "suggested_auto_skip_hours": None,
    }
    # Invalid verdict → None
    assert parse_llm_response('{"verdict": "panic", "reasoning": "..."}') is None
    # Malformed JSON → None
    assert parse_llm_response("not json") is None
    # Empty → None
    assert parse_llm_response("") is None


def test_parse_llm_response_strips_code_fences():
    """LLMs sometimes wrap JSON in ```json fences despite explicit
    instructions not to. Strip them."""
    from app.monitoring.ai_provider_supervisor import parse_llm_response
    raw = '```json\n{"verdict": "watch", "reasoning": "monitor"}\n```'
    out = parse_llm_response(raw)
    assert out is not None
    assert out["verdict"] == "watch"


def test_parse_llm_response_extracts_suggestion_fields():
    from app.monitoring.ai_provider_supervisor import parse_llm_response
    out = parse_llm_response(
        '{"verdict": "deprioritize", "reasoning": "high error rate", '
        '"suggested_priority_delta": 2, "suggested_auto_skip_hours": null}'
    )
    assert out["verdict"] == "deprioritize"
    assert out["suggested_priority_delta"] == 2


# ── Auto-apply caps ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_caps_priority_delta_to_setting():
    """suggested_priority_delta is capped by
    ai_provider_supervisor_max_priority_delta to prevent runaway."""
    from app.monitoring.ai_provider_supervisor import _apply_suggestion
    from app.config import settings
    fake_provider = MagicMock()
    fake_provider.id = "p1"
    fake_provider.priority = 5
    fake_provider.manual_override_until = None
    fake_review = MagicMock()
    fake_review.applied_action = None
    with patch.object(settings, "ai_provider_supervisor_max_priority_delta", 2):
        await _apply_suggestion(None, fake_provider, fake_review,
                                {"verdict": "deprioritize", "suggested_priority_delta": 10})
    # Capped at 2 → priority went from 5 to 7
    assert fake_provider.priority == 7
    assert "priority+=2" in fake_review.applied_action


@pytest.mark.asyncio
async def test_apply_caps_auto_skip_hours_to_setting():
    from app.monitoring.ai_provider_supervisor import _apply_suggestion
    from app.config import settings
    fake_provider = MagicMock()
    fake_provider.id = "p1"
    fake_provider.auto_skip_until = None
    fake_provider.manual_override_until = None
    fake_review = MagicMock()
    fake_review.applied_action = None
    with patch.object(settings, "ai_provider_supervisor_max_auto_skip_hours", 24):
        await _apply_suggestion(None, fake_provider, fake_review,
                                {"verdict": "disable", "suggested_auto_skip_hours": 168,
                                 "reasoning": "outage detected"})
    # Capped at 24h
    assert "auto_skip+=24h" in fake_review.applied_action


# ── Cluster sync ───────────────────────────────────────────────────


def test_cluster_manager_includes_provider_ai_reviews_in_payload():
    src = Path("app/cluster/manager.py").read_text()
    assert "ProviderAiReview" in src
    assert '"provider_ai_reviews"' in src


def test_cluster_sync_applies_provider_ai_reviews():
    src = Path("app/cluster/sync.py").read_text()
    assert "_apply_provider_ai_reviews" in src
    # Called from apply_sync
    assert 'payload.get("provider_ai_reviews", [])' in src


def test_apply_provider_ai_reviews_handles_lifecycle_monotone():
    """Applied/reverted/dismissed transitions are monotone (None → set).
    The merge code must respect this same as the api_key_ai_review path."""
    src = Path("app/cluster/sync.py").read_text()
    idx = src.index("async def _apply_provider_ai_reviews")
    body = src[idx:idx + 3000]
    for field in ('"applied_at"', '"reverted_at"', '"dismissed_at"'):
        assert field in body


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 31)
