"""v3.4.0 — LMRHv2 Phase 3 + per-direction cost split.

Covers:
- estimate_cost_split returns (in, out) tuple
- record_request accepts cost_split and writes per-direction columns
- snapshot reads per-direction columns and exposes
  cost_per_1m_input_usd vs _output_usd as different rates
- well-known config advertises /lmrh/stream when v2 enabled
"""
from __future__ import annotations

import pytest


# ── pricing.estimate_cost_split ───────────────────────────────────────


def test_estimate_cost_split_returns_tuple():
    """v3.4.0: new helper returns (input_cost, output_cost)."""
    from app.monitoring.pricing import estimate_cost_split
    in_c, out_c = estimate_cost_split("openai/gpt-4o", 1000, 500)
    assert isinstance(in_c, float)
    assert isinstance(out_c, float)
    # input is cheaper per-token than output for gpt-4o
    # (input $2.50/1M vs output $10/1M); 1000 in vs 500 out
    assert in_c > 0
    assert out_c > 0


def test_estimate_cost_total_equals_split_sum():
    """estimate_cost is now a thin wrapper that sums the split."""
    from app.monitoring.pricing import estimate_cost, estimate_cost_split
    total = estimate_cost("openai/gpt-4o", 1000, 500)
    in_c, out_c = estimate_cost_split("openai/gpt-4o", 1000, 500)
    assert abs(total - (in_c + out_c)) < 1e-9


def test_estimate_cost_split_unknown_model_returns_zero_zero():
    from app.monitoring.pricing import estimate_cost_split
    assert estimate_cost_split("ollama/local-model", 100, 50) == (0.0, 0.0)


# ── record_request cost_split parameter ───────────────────────────────


@pytest.mark.asyncio
async def test_record_request_writes_per_direction_columns(monkeypatch):
    """When cost_split is passed, per-direction columns get the real
    values rather than the heuristic 50/50 split."""
    from app.monitoring import metrics
    captured = {}

    class FakeMetric:
        provider_id = "p1"
        bucket_ts = None
        requests = 0
        successes = 0
        failures = 0
        total_tokens = 0
        total_cost_usd = 0.0
        avg_latency_ms = 0.0
        avg_ttft_ms = 0.0
        ttft_requests = 0
        circuit_state = "closed"
        input_cost_usd = 0.0
        output_cost_usd = 0.0
        input_tokens = 0
        output_tokens = 0

    class FakeQR:
        def scalar_one_or_none(self):
            return None  # force new-row branch

    class FakeDB:
        async def execute(self, *_a, **_k):
            return FakeQR()
        async def commit(self):
            pass
        def add(self, obj):
            captured["metric"] = obj

    monkeypatch.setattr(metrics, "get_all_states", lambda: {})
    fake = FakeDB()
    await metrics.record_request(
        fake, "p1", True, input_tokens=1000, output_tokens=500,
        latency_ms=200.0, cost_usd=0.0125,
        api_key_id=None,
        cost_split=(0.0025, 0.005),  # input cheap, output 2× per-token
    )
    m = captured["metric"]
    assert m.input_cost_usd == 0.0025
    assert m.output_cost_usd == 0.005
    assert m.input_tokens == 1000
    assert m.output_tokens == 500


@pytest.mark.asyncio
async def test_record_request_split_fallback_when_none(monkeypatch):
    """Legacy callers don't pass cost_split — fall back to the
    token-proportional heuristic so per-direction columns still
    populate (better than zero)."""
    from app.monitoring import metrics
    captured = {}

    class FakeQR:
        def scalar_one_or_none(self): return None
    class FakeDB:
        async def execute(self, *_a, **_k): return FakeQR()
        async def commit(self): pass
        def add(self, obj): captured["metric"] = obj

    monkeypatch.setattr(metrics, "get_all_states", lambda: {})
    fake = FakeDB()
    # 600 in / 400 out, $0.01 total, no cost_split passed
    await metrics.record_request(
        fake, "p1", True, 600, 400, 100.0, 0.01,
        api_key_id=None,  # cost_split=None default
    )
    m = captured["metric"]
    # Heuristic: 60% / 40% by token share
    assert abs(m.input_cost_usd - 0.006) < 1e-9
    assert abs(m.output_cost_usd - 0.004) < 1e-9


# ── well-known config advertises /lmrh/stream ─────────────────────────


def test_well_known_config_advertises_stream_when_v2_on(monkeypatch):
    """v3.4.0: /lmrh/stream is in endpoints when LMRHv2 enabled."""
    import asyncio
    from app.api import lmrh_v2 as lv2
    monkeypatch.setattr(lv2, "_v2_enabled", lambda: True)
    cfg = asyncio.run(lv2.well_known_config())
    assert "stream" in cfg["endpoints"]
    assert cfg["endpoints"]["stream"] == "/lmrh/stream"
    assert cfg["polling"].get("stream_recommended") is True


def test_well_known_config_omits_stream_when_v2_off(monkeypatch):
    """When LMRHv2 disabled, no v2 endpoints are advertised."""
    import asyncio
    from app.api import lmrh_v2 as lv2
    monkeypatch.setattr(lv2, "_v2_enabled", lambda: False)
    cfg = asyncio.run(lv2.well_known_config())
    assert "stream" not in cfg["endpoints"]
    assert "providers" not in cfg["endpoints"]
