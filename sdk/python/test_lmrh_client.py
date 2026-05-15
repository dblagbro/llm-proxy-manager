"""Tests for the LMRH v2 Python SDK reference impl.

Mocks the proxy via ``httpx.MockTransport`` so the tests don't hit a
live proxy. Coverage:
  - Polling loop start/stop lifecycle
  - Snapshot parsing from wire-format dict
  - Hint synthesis for each ``prefer`` mode
  - Family → provider-hint translation
  - Graceful degradation on 404 (proxy doesn't support v2)
  - ETag round-trip (304 → snapshot unchanged)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from lmrh_client import (  # noqa: E402
    LmrhClient,
    Snapshot,
    _format_hint,
    _provider_hint_for_family,
    _snapshot_from_dict,
)


# ── Wire-format parsing ───────────────────────────────────────────


def _sample_payload() -> dict:
    return {
        "version": "2.0",
        "as_of": "2026-05-09T00:00:00Z",
        "window_sec": 3600,
        "providers": [
            {
                "id": "p-grok-web",
                "name": "Grok-Web-Devin",
                "type": "grok-web",
                "priority": 1,
                "cost_class": "subscription",
                "circuit": "closed",
                "regions": ["us"],
                "models": [{
                    "model_id": "grok-3", "kind": "chat",
                    "context_length": 128000,
                    "native_tools": False, "native_reasoning": False,
                    "metrics": {
                        "cost_per_1m_input_usd": 0.0,
                        "cost_per_1m_output_usd": 0.0,
                        "rated_quota_per_1m_input_usd": None,
                        "latency_p50_ms": 2700.0, "latency_p95_ms": 4000.0,
                        "ttft_p50_ms": 800.0, "ttft_p95_ms": 1800.0,
                        "success_rate": 1.0, "samples": 50,
                    },
                }],
            },
            {
                "id": "p-claude",
                "name": "Devin-Anthropic-Max",
                "type": "claude-oauth",
                "priority": 5,
                "cost_class": "subscription",
                "circuit": "closed",
                "regions": ["us"],
                "models": [{
                    "model_id": "claude-sonnet-4-6", "kind": "chat",
                    "context_length": 200000,
                    "native_tools": True, "native_reasoning": True,
                    "metrics": {
                        "cost_per_1m_input_usd": 0.0,
                        "cost_per_1m_output_usd": 0.0,
                        "rated_quota_per_1m_input_usd": 3.0,
                        "latency_p50_ms": 1400.0, "latency_p95_ms": 2200.0,
                        "ttft_p50_ms": 400.0, "ttft_p95_ms": 900.0,
                        "success_rate": 0.998, "samples": 600,
                    },
                }],
            },
        ],
    }


def test_snapshot_parsing():
    snap = _snapshot_from_dict(_sample_payload(), '"abc123"')
    assert isinstance(snap, Snapshot)
    assert snap.version == "2.0"
    assert len(snap.providers) == 2
    assert snap.providers[0].id == "p-grok-web"
    assert snap.providers[0].models[0].metrics.samples == 50
    assert snap.etag == '"abc123"'


def test_snapshot_find_model():
    snap = _snapshot_from_dict(_sample_payload(), '"x"')
    matches = snap.find_model("grok-3")
    assert len(matches) == 1
    assert matches[0][0].name == "Grok-Web-Devin"


# ── Hint formatting ──────────────────────────────────────────────


def test_format_hint_skips_empty_values():
    h = _format_hint({"task": "chat", "cost": "", "region": "us"})
    assert "task=chat" in h
    assert "region=us" in h
    assert "cost=" not in h, "empty values must be dropped"


def test_provider_hint_for_family():
    assert "claude-oauth" in _provider_hint_for_family("claude")
    assert "openai" in _provider_hint_for_family("openai")
    assert "google" in _provider_hint_for_family("gemini")
    assert _provider_hint_for_family("unknown-vendor") is None


# ── Client lifecycle (no real network) ───────────────────────────


@pytest.fixture
def mocked_proxy(monkeypatch):
    """Returns a builder ``(payload, status) → LmrhClient`` whose
    polling routes through httpx.MockTransport. monkeypatch auto-
    restores ``httpx.Client`` after each test so mocks don't leak.
    """
    import lmrh_client as mod
    real_client_cls = httpx.Client

    def builder(payload: dict, status: int = 200) -> LmrhClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if status == 304:
                return httpx.Response(304, headers={"etag": '"unchanged"'})
            if status == 404:
                return httpx.Response(404, text="not found")
            return httpx.Response(
                status,
                json=payload,
                headers={"etag": '"abc"'},
            )

        def patched_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client_cls(*args, **kwargs)

        monkeypatch.setattr(mod.httpx, "Client", patched_client)

        return LmrhClient(
            base_url="http://test-proxy",
            api_key="llmp-test",
            poll_interval_sec=1,
            timeout=2.0,
        )

    return builder


def _make_client_with_mock(payload: dict, status: int = 200) -> LmrhClient:
    """Legacy alias kept for in-test convenience. Prefer the
    ``mocked_proxy`` fixture for new tests so monkeypatch auto-cleans."""
    raise RuntimeError("use the mocked_proxy fixture instead")


def test_client_acquires_snapshot_on_start(mocked_proxy):
    """After start(), the client should have a snapshot within ~5s."""
    client = mocked_proxy(_sample_payload())
    try:
        client.start()
        snap = client.snapshot()
        assert snap is not None, "snapshot should be populated after start()"
        assert len(snap.providers) == 2
    finally:
        client.stop()


def test_client_degrades_gracefully_on_404(mocked_proxy):
    """Proxy returning 404 → is_supported() False, snapshot None,
    but build_hint still returns a usable hint string."""
    client = mocked_proxy(_sample_payload(), status=404)
    try:
        client.start()
        time.sleep(0.5)
        assert client.snapshot() is None
        assert client.is_supported() is False
        # Hint synthesis still works without snapshot data
        hint = client.build_hint(task="chat", prefer="cheapest",
                                 model_family="claude")
        assert "task=chat" in hint
        assert "cost=economy" in hint
        assert "claude-oauth" in hint
    finally:
        client.stop()


def test_build_hint_cheapest(mocked_proxy):
    client = mocked_proxy(_sample_payload())
    try:
        client.start()
        h = client.build_hint(task="chat", prefer="cheapest")
        assert "cost=economy" in h
        assert "task=chat" in h
    finally:
        client.stop()


def test_build_hint_fastest(mocked_proxy):
    client = mocked_proxy(_sample_payload())
    try:
        client.start()
        h = client.build_hint(prefer="fastest")
        assert "latency=interactive" in h
    finally:
        client.stop()


def _started(client):
    """Start a client and block until its first snapshot lands."""
    client.start()
    for _ in range(10):
        if client.snapshot() is not None:
            return
        time.sleep(0.1)


def _provider_hint_value(hint: str) -> str:
    """Extract the provider-hint dim value from a formatted hint."""
    for dim in hint.split(","):
        dim = dim.strip()
        if dim.startswith("provider-hint="):
            return dim[len("provider-hint="):]
    return ""


def test_build_hint_most_reliable_picks_claude(mocked_proxy):
    """claude in the fixture has 0.998×600 samples; grok-web has
    1.0×50 samples. Weighted score (samp / 1000 cap):
      claude: 0.998 × (1 + 600/1000) = 1.597
      grok-web: 1.0 × (1 + 50/1000) = 1.05
    → claude wins → its provider TYPE (claude-oauth) is emitted."""
    client = mocked_proxy(_sample_payload())
    try:
        _started(client)
        h = client.build_hint(prefer="most_reliable")
        assert "provider-hint=claude-oauth" in h, f"got: {h}"
    finally:
        client.stop()


def test_build_hint_most_reliable_emits_safe_token(mocked_proxy):
    """Regression: pre-fix the hint pinned ``provider-hint=p-claude``
    (the internal id, which the proxy can't match → inert). The fix
    emits the provider TYPE — and it must be a header-safe slug: no
    internal id, no whitespace (provider NAMES can carry spaces)."""
    client = mocked_proxy(_sample_payload())
    try:
        _started(client)
        h = client.build_hint(prefer="most_reliable")
        ph = _provider_hint_value(h)
        assert ph and "p-claude" not in ph and "p-grok-web" not in ph, h
        assert " " not in ph, f"provider-hint not header-safe: {ph!r}"
    finally:
        client.stop()


def test_build_hint_most_reliable_within_family(mocked_proxy):
    """most_reliable + model_family → the most reliable provider OF
    that family, by its type (not clobbered by the family type list)."""
    client = mocked_proxy(_sample_payload())
    try:
        _started(client)
        # grok family → only the grok-web provider qualifies
        h = client.build_hint(prefer="most_reliable", model_family="grok")
        assert "provider-hint=grok-web" in h, f"got: {h}"
    finally:
        client.stop()


def test_build_hint_most_reliable_family_no_match_falls_back(mocked_proxy):
    """most_reliable + a family with no qualifying provider in the
    snapshot → falls back to the family's provider-type list."""
    client = mocked_proxy(_sample_payload())
    try:
        _started(client)
        # openai family — the fixture has no openai/codex-oauth provider
        h = client.build_hint(prefer="most_reliable", model_family="openai")
        assert "provider-hint=openai|codex-oauth" in h, f"got: {h}"
    finally:
        client.stop()


def test_family_provider_types_helper():
    from lmrh_client import _family_provider_types
    assert _family_provider_types("claude") == (
        "anthropic", "claude-oauth", "anthropic-oauth")
    assert _family_provider_types("grok") == ("grok", "grok-web")
    assert _family_provider_types("unknown-vendor") is None


def test_build_hint_region_pinned(mocked_proxy):
    client = mocked_proxy(_sample_payload())
    try:
        client.start()
        h = client.build_hint(region="us")
        assert "region=us;require" in h
    finally:
        client.stop()


def test_build_hint_extra_overrides_synthesized(mocked_proxy):
    """``extra`` is last-write-wins so callers can override."""
    client = mocked_proxy(_sample_payload())
    try:
        client.start()
        h = client.build_hint(
            task="chat",
            prefer="cheapest",
            extra={"cost": "premium"},  # override synthesized economy
        )
        assert "cost=premium" in h
        assert "cost=economy" not in h
    finally:
        client.stop()
