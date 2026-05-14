"""v3.9.2 (#268) — 1% sample-rate full request_body capture on 4xx.

Covers ``app.monitoring.helpers._maybe_sample_4xx_body`` in isolation:
- No-op on non-bad_request error_classes (auth, rate_limit, timeout, etc)
- No-op when sample rate is 0
- Always fires when sample rate is 1.0
- Roughly hits the configured rate at intermediate values
- Adds body_sampled tag when it fires
- Reuses existing request_body if already captured (capture_bodies=True path)
- Doesn't blow up on None request_body
- Source-level guard: the wire-up call exists in record_outcome's error branch
"""
from __future__ import annotations

import random
from unittest.mock import patch

from app.monitoring.helpers import _maybe_sample_4xx_body


def _body() -> dict:
    return {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }


# ── No-op cases ─────────────────────────────────────────────────────


def test_noop_for_auth_error_class():
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 1.0
        s.activity_log_max_body_chars = 4000
        meta = {}
        out = _maybe_sample_4xx_body(meta, _body(), "auth")
        assert out is meta
        assert "request_body" not in out
        assert "body_sampled" not in out


def test_noop_for_rate_limit_error_class():
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 1.0
        s.activity_log_max_body_chars = 4000
        out = _maybe_sample_4xx_body({}, _body(), "rate_limit")
        assert "request_body" not in out


def test_noop_for_billing_error_class():
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 1.0
        s.activity_log_max_body_chars = 4000
        out = _maybe_sample_4xx_body({}, _body(), "billing")
        assert "request_body" not in out


def test_noop_for_upstream_5xx():
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 1.0
        s.activity_log_max_body_chars = 4000
        out = _maybe_sample_4xx_body({}, _body(), "upstream_5xx")
        assert "request_body" not in out


def test_noop_for_unknown_error_class():
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 1.0
        s.activity_log_max_body_chars = 4000
        out = _maybe_sample_4xx_body({}, _body(), "unknown")
        assert "request_body" not in out


def test_noop_for_none_error_class():
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 1.0
        s.activity_log_max_body_chars = 4000
        out = _maybe_sample_4xx_body({}, _body(), None)
        assert "request_body" not in out


def test_noop_when_sample_rate_zero():
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 0.0
        s.activity_log_max_body_chars = 4000
        out = _maybe_sample_4xx_body({}, _body(), "bad_request")
        assert "request_body" not in out
        assert "body_sampled" not in out


def test_noop_when_sample_rate_missing():
    """Missing config falls back to 0 — safe default."""
    class _S:
        activity_log_max_body_chars = 4000
        # activity_log_body_sample_rate_4xx intentionally absent
    with patch("app.monitoring.helpers.settings", _S()):
        out = _maybe_sample_4xx_body({}, _body(), "bad_request")
        assert "request_body" not in out


def test_noop_when_request_body_none():
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 1.0
        s.activity_log_max_body_chars = 4000
        out = _maybe_sample_4xx_body({}, None, "bad_request")
        assert "request_body" not in out
        assert "body_sampled" not in out


# ── Capture cases ──────────────────────────────────────────────────


def test_always_captures_at_rate_1_0():
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 1.0
        s.activity_log_max_body_chars = 4000
        out = _maybe_sample_4xx_body({}, _body(), "bad_request")
        assert "request_body" in out
        assert out["body_sampled"] is True
        # the serialized body should mention the model name
        assert "claude-haiku-4-5-20251001" in out["request_body"]


def test_body_truncated_to_max_chars():
    huge = {"messages": [{"role": "user", "content": "X" * 10000}]}
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 1.0
        s.activity_log_max_body_chars = 1500
        out = _maybe_sample_4xx_body({}, huge, "bad_request")
        # cap is enforced in _serialize_body
        assert len(out["request_body"]) <= 1500


def test_only_tags_when_body_already_captured():
    """When activity_log_capture_bodies=True has already filled in
    request_body via _attach_bodies, the sampler should NOT overwrite —
    just add the body_sampled marker so the hub UI can filter on it."""
    pre_filled = {"request_body": "<<already captured by _attach_bodies>>"}
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 1.0
        s.activity_log_max_body_chars = 4000
        out = _maybe_sample_4xx_body(pre_filled, _body(), "bad_request")
        assert out["request_body"] == "<<already captured by _attach_bodies>>"
        assert out["body_sampled"] is True


def test_rate_distribution_roughly_correct():
    """At rate=0.5 across 2000 trials, captures should cluster around
    1000 ± ~80 (3-sigma binomial). Wide tolerance to keep CI stable."""
    random.seed(42)
    hits = 0
    with patch("app.monitoring.helpers.settings") as s:
        s.activity_log_body_sample_rate_4xx = 0.5
        s.activity_log_max_body_chars = 4000
        for _ in range(2000):
            out = _maybe_sample_4xx_body({}, _body(), "bad_request")
            if out.get("body_sampled"):
                hits += 1
    assert 800 <= hits <= 1200, f"Expected ~1000 hits at rate=0.5, got {hits}"


# ── Source-level wiring guards ─────────────────────────────────────


def test_wired_into_record_outcome_error_branch():
    """Ensure the sampler is actually called from the error branch."""
    from pathlib import Path
    src = Path("app/monitoring/helpers.py").read_text()
    # Error branch attaches bodies then sample
    assert "_attach_bodies(meta, request_body, response_body)" in src
    assert "_maybe_sample_4xx_body(meta, request_body, meta[\"error_class\"])" in src
    # Order matters: _maybe_sample_4xx_body must come AFTER _attach_bodies
    # (so it can detect pre-existing request_body and only tag).
    idx_attach = src.find("_attach_bodies(meta, request_body, response_body)")
    idx_sample = src.find("_maybe_sample_4xx_body(meta, request_body, meta[\"error_class\"])")
    assert idx_attach > 0 and idx_sample > idx_attach


def test_config_settings_exist():
    from app.config import settings
    val = getattr(settings, "activity_log_body_sample_rate_4xx", None)
    assert val is not None
    assert 0.0 <= float(val) <= 1.0


def test_runtime_schema_exposes_setting():
    from app.config_runtime import SCHEMA
    assert "activity_log_body_sample_rate_4xx" in SCHEMA
    assert SCHEMA["activity_log_body_sample_rate_4xx"]["type"] == "float"
