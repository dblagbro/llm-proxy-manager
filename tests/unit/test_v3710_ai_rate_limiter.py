"""v3.7.10 — AI rate limiter tests."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest

from app.monitoring.ai_rate_limiter import (
    _redact,
    _percentile,
    compute_stats,
    pick_sample_previews,
    build_prompt,
    parse_llm_response,
    _verdict_to_action,
)


# ── _redact ────────────────────────────────────────────────────────


def test_redact_anthropic_token():
    out = _redact("Auth: sk-ant-oat01-abc123xyz")
    assert "sk-ant-oat01" not in out
    assert "REDACTED-ANTHROPIC-TOKEN" in out


def test_redact_openai_token():
    out = _redact("key sk-1234567890abcdefghij1234567890")
    assert "sk-1234567890" not in out
    assert "REDACTED-OPENAI-TOKEN" in out


def test_redact_llmp_key():
    out = _redact("hdr: llmp-abc123def456ghi789jkl012mno345")
    assert "llmp-abc" not in out
    assert "REDACTED-LLMP-KEY" in out


def test_redact_google_key():
    out = _redact("token AIza" + "x" * 35)
    assert "REDACTED-GOOGLE-KEY" in out


def test_redact_caps_length():
    """Even after redaction the preview must be capped to ~300 chars."""
    out = _redact("a" * 1000)
    assert len(out) <= 300


def test_redact_handles_clean_text():
    """No tokens → text passes through (truncated only)."""
    out = _redact("just a normal request body")
    assert out == "just a normal request body"


# ── _percentile ────────────────────────────────────────────────────


def test_percentile_empty():
    assert _percentile([], 0.5) is None


def test_percentile_basic():
    assert _percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert _percentile([1, 2, 3, 4, 5], 0.95) == 5


def test_percentile_handles_floats():
    assert _percentile([1.5, 2.5, 3.5], 0.5) == 2.5


# ── compute_stats ──────────────────────────────────────────────────


def _event(severity="info", in_tok=100, out_tok=50, latency_ms=500,
           ip="192.168.1.1", model="claude-haiku-4-5", cost_class="subscription",
           preview="hello"):
    return {
        "timestamp": "2026-05-10T12:00:00Z",
        "event_type": "llm_request",
        "severity": severity,
        "message": "X",
        "metadata": {
            "in_tok": in_tok,
            "out_tok": out_tok,
            "latency_ms": latency_ms,
            "client_ip": ip,
            "model": model,
            "cost_class": cost_class,
            "request_preview": preview,
        },
    }


def test_compute_stats_empty():
    s = compute_stats([])
    assert s["total_requests"] == 0
    assert s["error_count"] == 0
    assert s["unique_ips"] == 0


def test_compute_stats_basic():
    events = [_event() for _ in range(10)]
    s = compute_stats(events)
    assert s["total_requests"] == 10
    assert s["error_count"] == 0
    assert s["unique_ips"] == 1
    assert s["unique_models"] == 1
    assert s["p50_input_tokens"] == 100
    assert s["p50_output_tokens"] == 50
    assert s["p50_latency_ms"] == 500


def test_compute_stats_counts_errors():
    events = [_event(severity="info") for _ in range(8)]
    events += [_event(severity="error") for _ in range(2)]
    s = compute_stats(events)
    assert s["total_requests"] == 10
    assert s["error_count"] == 2
    assert s["error_rate_pct"] == 20.0


def test_compute_stats_multiple_ips_and_models():
    events = [
        _event(ip="1.1.1.1", model="m1"),
        _event(ip="1.1.1.2", model="m1"),
        _event(ip="1.1.1.1", model="m2"),
    ]
    s = compute_stats(events)
    assert s["unique_ips"] == 2
    assert s["unique_models"] == 2


def test_compute_stats_prompt_size_variance():
    """Highly variable prompt sizes should produce a non-trivial CV."""
    events = [_event(in_tok=10) for _ in range(3)]
    events += [_event(in_tok=10000) for _ in range(3)]
    s = compute_stats(events)
    assert s["prompt_size_variance_pct"] is not None
    assert s["prompt_size_variance_pct"] > 50.0  # huge variance


def test_compute_stats_uniform_prompt_size_low_variance():
    events = [_event(in_tok=1000) for _ in range(10)]
    s = compute_stats(events)
    # Identical sizes → variance ~ 0
    assert s["prompt_size_variance_pct"] == 0.0


def test_compute_stats_cost_class_distribution():
    events = [_event(cost_class="subscription") for _ in range(7)]
    events += [_event(cost_class="per_call") for _ in range(3)]
    s = compute_stats(events)
    assert s["cost_class_dist"]["subscription"] == 7
    assert s["cost_class_dist"]["per_call"] == 3


def test_compute_stats_handles_missing_metadata():
    events = [{"timestamp": "2026-05-10T12:00:00Z", "event_type": "x",
               "severity": "info", "message": "", "metadata": None}]
    # Should not raise
    s = compute_stats(events)
    assert s["total_requests"] == 1


# ── pick_sample_previews ───────────────────────────────────────────


def test_pick_samples_returns_up_to_n():
    events = [_event(preview=f"preview-{i}") for i in range(10)]
    samples = pick_sample_previews(events, n=3)
    assert len(samples) == 3


def test_pick_samples_returns_one_for_single_event():
    events = [_event(preview="solo")]
    samples = pick_sample_previews(events)
    assert len(samples) == 1
    assert "solo" in samples[0]


def test_pick_samples_empty_for_empty_events():
    assert pick_sample_previews([]) == []


def test_pick_samples_redacts_tokens():
    events = [_event(preview="contains sk-ant-oat01-abc123")]
    samples = pick_sample_previews(events)
    assert "sk-ant-oat01" not in samples[0]
    assert "REDACTED" in samples[0]


# ── build_prompt ───────────────────────────────────────────────────


def test_build_prompt_includes_stats_and_samples():
    stats = compute_stats([_event() for _ in range(5)])
    samples = ["sample1", "sample2"]
    prompt = build_prompt(stats, samples, "test-key")
    assert "test-key" in prompt
    assert "Total requests: 5" in prompt
    assert "sample1" in prompt
    assert "sample2" in prompt
    # Verdict options enumerated
    for v in ("normal", "watch", "throttle", "block"):
        assert v in prompt


def test_build_prompt_handles_no_samples():
    stats = compute_stats([])
    prompt = build_prompt(stats, [], "no-traffic-key")
    assert "(none captured)" in prompt


# ── parse_llm_response ─────────────────────────────────────────────


def test_parse_clean_json():
    text = '{"verdict": "throttle", "reasoning": "high error rate"}'
    out = parse_llm_response(text)
    assert out["verdict"] == "throttle"


def test_parse_handles_markdown_wrap():
    text = '```json\n{"verdict": "normal", "reasoning": "looks fine"}\n```'
    out = parse_llm_response(text)
    assert out is not None
    assert out["verdict"] == "normal"


def test_parse_handles_preamble():
    text = 'Sure thing! Here is my analysis:\n\n{"verdict": "watch", "reasoning": "slightly elevated"}'
    out = parse_llm_response(text)
    assert out is not None
    assert out["verdict"] == "watch"


def test_parse_returns_none_for_garbage():
    assert parse_llm_response("not json at all") is None
    assert parse_llm_response("") is None


# ── _verdict_to_action ─────────────────────────────────────────────


def test_verdict_to_action_mapping():
    assert _verdict_to_action("throttle") == "throttle_rpm"
    assert _verdict_to_action("block") == "disable"
    assert _verdict_to_action("normal") == "none"
    assert _verdict_to_action("watch") == "none"
    assert _verdict_to_action("unknown") == "none"


# ── Wiring regression ─────────────────────────────────────────────


def test_main_lifespan_starts_ai_rate_limiter():
    from pathlib import Path
    src = Path("app/main.py").read_text()
    assert "ai_rate_limiter" in src
    assert "_ai_rl.start()" in src


def test_admin_router_registered():
    from pathlib import Path
    src = Path("app/main.py").read_text()
    assert "ai_rate_limiter_router" in src
    assert "include_router(ai_rate_limiter_router)" in src


def test_settings_have_ai_rate_limiter_fields():
    from app.config import settings
    assert hasattr(settings, "ai_rate_limiter_enabled")
    assert hasattr(settings, "ai_rate_limiter_interval_sec")
    assert hasattr(settings, "ai_rate_limiter_model")
    assert hasattr(settings, "ai_rate_limiter_throttle_floor_rpm")
    assert hasattr(settings, "ai_rate_limiter_auto_apply")
    # Defaults per operator Q5: opt-in (False) + suggest-only (False)
    assert settings.ai_rate_limiter_enabled is False
    assert settings.ai_rate_limiter_auto_apply is False


def test_api_key_ai_review_model_exists():
    from app.models.db import ApiKeyAiReview
    cols = {c.name for c in ApiKeyAiReview.__table__.columns}
    expected = {
        "id", "api_key_id", "captured_at", "llm_model", "llm_verdict",
        "llm_reasoning", "suggested_action", "stats_summary",
        "applied_at", "applied_action", "prior_rate_limit_rpm",
        "reverted_at", "dismissed_at",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"
