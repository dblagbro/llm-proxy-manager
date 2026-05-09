"""v3.3.4 — probe event_type split + LMRHv2 probe channel surfacing.

#3 record_outcome writes event_type='keepalive_probe' for probes (was
   'llm_request' with [probe] msg prefix pre-v3.3.4)
#4 snapshot computes probe_success_rate / probe_samples from
   activity_log keepalive_probe rows; /lmrh/providers and SDK both
   carry the new fields with backward-compat None defaults
"""
from __future__ import annotations

import pytest


# ── #3 event_type split ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_outcome_probe_uses_keepalive_probe_event_type(monkeypatch):
    """Probe-keyed outcomes log with event_type='keepalive_probe'."""
    from app.monitoring import helpers

    seen = []

    async def fake_log_event(db, *, event_type, message, severity,
                              provider_id, api_key_id, metadata):
        seen.append({
            "event_type": event_type,
            "severity": severity,
            "is_probe_meta": metadata.get("probe", False),
        })

    async def fake_record_request(*_a, **_k): pass
    async def fake_record_cost(*_a, **_k): pass
    async def fake_record_success(*_a, **_k): pass
    async def fake_record_failure(*_a, **_k): pass

    monkeypatch.setattr(helpers, "log_event", fake_log_event)
    monkeypatch.setattr(helpers, "record_request", fake_record_request)
    monkeypatch.setattr(helpers, "record_cost", fake_record_cost)
    monkeypatch.setattr(helpers, "record_success", fake_record_success)
    monkeypatch.setattr(helpers, "record_failure", fake_record_failure)
    monkeypatch.setattr(helpers, "observe_request", lambda **_k: None)
    monkeypatch.setattr(helpers, "estimate_cost", lambda *_a: 0.0)
    monkeypatch.setattr(helpers, "clear_auth_failure", lambda _p: None)

    class FakeProvider:
        provider_type = "grok-web"
        cost_class = "subscription"
    class FakeDB:
        async def get(self, _model, _id): return FakeProvider()

    fake = FakeDB()

    # Success path
    await helpers.record_outcome(
        fake, provider_id="p1", model="grok-3",
        success=True, in_tok=10, out_tok=5,
        t0=0.0, key_record_id="probe-keepalive",
        provider_name="Grok",
    )
    assert len(seen) == 1
    assert seen[0]["event_type"] == "keepalive_probe"
    assert seen[0]["severity"] == "info"
    assert seen[0]["is_probe_meta"] is True

    # Failure path
    await helpers.record_outcome(
        fake, provider_id="p1", model="grok-3",
        success=False, error_str="HTTP 429", t0=0.0,
        key_record_id="probe-keepalive",
        provider_name="Grok",
    )
    assert len(seen) == 2
    assert seen[1]["event_type"] == "keepalive_probe"
    assert seen[1]["severity"] == "warning"


@pytest.mark.asyncio
async def test_record_outcome_user_traffic_keeps_llm_request_event_type(monkeypatch):
    """Non-probe (real-key) outcomes still use event_type='llm_request'.
    Catches accidental over-rotation of the new event_type."""
    from app.monitoring import helpers

    seen = []

    async def fake_log_event(db, *, event_type, message, severity,
                              provider_id, api_key_id, metadata):
        seen.append({"event_type": event_type, "severity": severity})

    async def fake_record_request(*_a, **_k): pass
    async def fake_record_cost(*_a, **_k): pass
    async def fake_record_success(*_a, **_k): pass

    monkeypatch.setattr(helpers, "log_event", fake_log_event)
    monkeypatch.setattr(helpers, "record_request", fake_record_request)
    monkeypatch.setattr(helpers, "record_cost", fake_record_cost)
    monkeypatch.setattr(helpers, "record_success", fake_record_success)
    monkeypatch.setattr(helpers, "observe_request", lambda **_k: None)
    monkeypatch.setattr(helpers, "estimate_cost", lambda *_a: 0.001)
    monkeypatch.setattr(helpers, "clear_auth_failure", lambda _p: None)

    class FakeProvider:
        provider_type = "openrouter"
        cost_class = "per_call"
    class FakeApiKey:
        key_prefix = "llmp-XXXX"
    class FakeDB:
        async def get(self, model, _id):
            from app.models.db import ApiKey
            return FakeApiKey() if model is ApiKey else FakeProvider()

    fake = FakeDB()
    await helpers.record_outcome(
        fake, provider_id="p1", model="claude-sonnet",
        success=True, in_tok=100, out_tok=50, t0=0.0,
        key_record_id="real-user-key-id",
        provider_name="OpenRouter",
    )
    assert len(seen) == 1
    assert seen[0]["event_type"] == "llm_request"


# ── #4 snapshot probe stats from activity_log ─────────────────────────


def test_modelmetrics_dataclass_has_probe_fields():
    """SDK ModelMetrics has the new optional fields with sane defaults."""
    from sdk.python.lmrh_client import ModelMetrics
    m = ModelMetrics(
        cost_per_1m_input_usd=None,
        cost_per_1m_output_usd=None,
        rated_quota_per_1m_input_usd=None,
        latency_p50_ms=100.0,
        latency_p95_ms=300.0,
        ttft_p50_ms=None,
        ttft_p95_ms=None,
        success_rate=0.99,
        samples=200,
    )
    # New fields default to None / 0 → backward compat with older proxies
    assert m.probe_success_rate is None
    assert m.probe_samples == 0


def test_sdk_parses_probe_fields_when_proxy_emits_them():
    """Wire-format → typed conversion picks up the new fields."""
    from sdk.python.lmrh_client import _snapshot_from_dict as _parse_snapshot
    wire = {
        "version": "2.0",
        "as_of": "2026-05-09T17:00:00+00:00",
        "window_sec": 3600,
        "providers": [
            {
                "id": "p1", "name": "Grok-Web", "type": "grok-web",
                "priority": 1, "cost_class": "subscription",
                "circuit": "closed", "regions": [],
                "models": [
                    {
                        "model_id": "grok-3", "kind": "chat",
                        "context_length": 128000,
                        "native_tools": False, "native_reasoning": False,
                        "metrics": {
                            "cost_per_1m_input_usd": None,
                            "cost_per_1m_output_usd": None,
                            "rated_quota_per_1m_input_usd": None,
                            "latency_p50_ms": 2500.0,
                            "latency_p95_ms": 6800.0,
                            "ttft_p50_ms": None,
                            "ttft_p95_ms": None,
                            "success_rate": 1.0,
                            "samples": 154,
                            "probe_success_rate": 0.74,
                            "probe_samples": 23,
                        },
                    }
                ],
            }
        ],
    }
    snap = _parse_snapshot(wire, etag='"abc"')
    assert len(snap.providers) == 1
    m = snap.providers[0].models[0]
    assert m.metrics.success_rate == 1.0
    assert m.metrics.samples == 154
    assert m.metrics.probe_success_rate == 0.74
    assert m.metrics.probe_samples == 23


def test_sdk_handles_proxy_without_probe_fields():
    """Older proxies don't emit probe_success_rate / probe_samples; SDK
    must degrade gracefully (no KeyError, no exception)."""
    from sdk.python.lmrh_client import _snapshot_from_dict as _parse_snapshot
    wire = {
        "version": "2.0",
        "as_of": "2026-05-09T17:00:00+00:00",
        "window_sec": 3600,
        "providers": [
            {
                "id": "p1", "name": "Old-Proxy", "type": "openai",
                "priority": 5, "cost_class": "per_call",
                "circuit": "closed", "regions": [],
                "models": [
                    {
                        "model_id": "gpt-4o", "kind": "chat",
                        "context_length": 128000,
                        "native_tools": True, "native_reasoning": False,
                        "metrics": {
                            "cost_per_1m_input_usd": 5.0,
                            "cost_per_1m_output_usd": 15.0,
                            "rated_quota_per_1m_input_usd": None,
                            "latency_p50_ms": 800.0,
                            "latency_p95_ms": 2000.0,
                            "ttft_p50_ms": None,
                            "ttft_p95_ms": None,
                            "success_rate": 0.99,
                            "samples": 500,
                            # NO probe_success_rate / probe_samples
                        },
                    }
                ],
            }
        ],
    }
    snap = _parse_snapshot(wire, etag='"old"')
    m = snap.providers[0].models[0]
    assert m.metrics.success_rate == 0.99
    # Defaults applied
    assert m.metrics.probe_success_rate is None
    assert m.metrics.probe_samples == 0
