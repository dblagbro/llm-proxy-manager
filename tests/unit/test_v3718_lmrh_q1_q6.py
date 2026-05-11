"""v3.7.18 — LMRHv2 Q1 (public endpoint) + Q6 (per-node override)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest


# ── Q6: per-node env-var override ─────────────────────────────────


def test_settings_has_node_override_field():
    from app.config import settings
    assert hasattr(settings, "lmrh_v2_node_override")
    # Default is "auto" (follow SystemSetting)
    assert isinstance(settings.lmrh_v2_node_override, str)


def test_v2_enabled_override_on_wins():
    """LMRH_V2_NODE_OVERRIDE=on must enable LMRH v2 even when the
    cluster-synced setting is False."""
    from app.api.lmrh_v2 import _v2_enabled
    with patch("app.api.lmrh_v2.settings") as s:
        s.lmrh_v2_node_override = "on"
        s.lmrh_v2_enabled = False
        assert _v2_enabled() is True


def test_v2_enabled_override_off_wins():
    """LMRH_V2_NODE_OVERRIDE=off must disable LMRH v2 even when the
    cluster-synced setting is True."""
    from app.api.lmrh_v2 import _v2_enabled
    with patch("app.api.lmrh_v2.settings") as s:
        s.lmrh_v2_node_override = "off"
        s.lmrh_v2_enabled = True
        assert _v2_enabled() is False


def test_v2_enabled_override_auto_follows_cluster():
    """Default 'auto' falls back to the cluster SystemSetting."""
    from app.api.lmrh_v2 import _v2_enabled
    with patch("app.api.lmrh_v2.settings") as s:
        s.lmrh_v2_node_override = "auto"
        s.lmrh_v2_enabled = True
        assert _v2_enabled() is True
        s.lmrh_v2_enabled = False
        assert _v2_enabled() is False


def test_override_string_case_insensitive():
    """ON/On/on all work — tolerate case."""
    from app.api.lmrh_v2 import _v2_enabled
    for val in ("ON", "On", "on", "  ON  "):
        with patch("app.api.lmrh_v2.settings") as s:
            s.lmrh_v2_node_override = val
            s.lmrh_v2_enabled = False
            assert _v2_enabled() is True, f"override={val!r} did not enable"


# ── Q1: public/no-auth view ───────────────────────────────────────


def _make_snap():
    """Synthesize a minimal LmrhSnapshot fixture covering 2 providers,
    1 model each (one shared route + one subscription route)."""
    from app.routing.lmrh.snapshot import (
        LmrhSnapshot, _ProviderSnap, _ModelSnap,
    )
    m1 = _ModelSnap(
        model_id="claude-sonnet-4-6", kind="chat", context_length=200000,
        native_tools=True, native_reasoning=False,
        cost_per_1m_input_usd=3.0, cost_per_1m_output_usd=15.0,
        rated_quota_per_1m_input_usd=None,
        latency_p50_ms=1200, latency_p95_ms=2800,
        ttft_p50_ms=400, ttft_p95_ms=900,
        success_rate=0.99, samples=120,
        aliases=["claude-sonnet-4-6"],
        family="anthropic-sonnet-4", variant="direct",
    )
    m2 = _ModelSnap(
        model_id="claude-sonnet-4-6", kind="chat", context_length=200000,
        native_tools=True, native_reasoning=False,
        cost_per_1m_input_usd=0.0, cost_per_1m_output_usd=0.0,
        rated_quota_per_1m_input_usd=15.0,
        latency_p50_ms=1500, latency_p95_ms=3200,
        ttft_p50_ms=500, ttft_p95_ms=1100,
        success_rate=0.98, samples=84,
        aliases=["claude-sonnet-4-6"],
        family="anthropic-sonnet-4", variant="oauth",
    )
    p1 = _ProviderSnap(
        id="abc123", name="Anthropic-Direct", type="anthropic",
        priority=1, cost_class="per_call", circuit="closed",
        regions=["us"], owned_by_key_id=None, models=[m1],
        subscription_quota=None,
    )
    p2 = _ProviderSnap(
        id="def456", name="Anthropic-Max-Gmail", type="claude-oauth",
        priority=2, cost_class="subscription", circuit="closed",
        regions=["us"], owned_by_key_id=None, models=[m2],
        subscription_quota=None,
    )
    return LmrhSnapshot(
        as_of=datetime.now(timezone.utc),
        window_sec=3600, providers=[p1, p2], etag="W/\"test\"",
    )


def test_public_view_omits_operator_provider_names():
    """The redacted view must NOT contain operator-internal names like
    'Anthropic-Max-Gmail' anywhere in the response."""
    import json
    snap = _make_snap()
    out = snap.to_public_view()
    body = json.dumps(out)
    assert "Anthropic-Max-Gmail" not in body
    assert "abc123" not in body  # internal id
    assert "def456" not in body


def test_public_view_aggregates_by_model():
    snap = _make_snap()
    out = snap.to_public_view()
    # Both providers serve claude-sonnet-4-6 — should coalesce to 1 entry
    assert out["models_count"] == 1
    m = out["models"][0]
    assert m["model_id"] == "claude-sonnet-4-6"
    assert m["family"] == "anthropic-sonnet-4"
    # variants from both routes
    assert sorted(m["variants"]) == ["direct", "oauth"]


def test_public_view_exposes_cost_tiers_not_numbers():
    """Coarse tier buckets only — no exact $/1M numbers."""
    import json
    snap = _make_snap()
    out = snap.to_public_view()
    body = json.dumps(out)
    # No exact prices
    assert "3.0" not in body or "$3" not in body
    # Tier strings present
    tiers = out["models"][0]["cost_tiers"]
    assert "subscription" in tiers
    assert any(t in tiers for t in ("economy", "standard", "premium"))


def test_public_view_exposes_redundancy_buckets_not_counts():
    """The exact number of routes is hidden — only bucket label."""
    snap = _make_snap()
    out = snap.to_public_view()
    m = out["models"][0]
    assert m["redundancy"] in ("none", "single", "few", "many")
    # 2 routes both healthy → "few"
    assert m["redundancy"] == "few"


def test_public_view_marks_auth_required():
    """Tells the client to come back with an API key for the full view."""
    snap = _make_snap()
    out = snap.to_public_view()
    assert out["auth_required_for_full_view"] is True
    assert "/lmrh/providers" in out["auth_endpoint"]


def test_public_view_scope_marker():
    snap = _make_snap()
    out = snap.to_public_view()
    assert out["scope"] == "public"
    assert out["version"] == "2.0"


def test_public_view_capabilities_exposed():
    snap = _make_snap()
    out = snap.to_public_view()
    cap = out["models"][0]["capabilities"]
    assert cap["context_length"] == 200000
    assert cap["native_tools"] is True
    assert cap["native_reasoning"] is False


def test_public_view_empty_snapshot_handles_no_providers():
    from app.routing.lmrh.snapshot import LmrhSnapshot
    snap = LmrhSnapshot(
        as_of=datetime.now(timezone.utc),
        window_sec=3600, providers=[], etag="W/\"empty\"",
    )
    out = snap.to_public_view()
    assert out["models_count"] == 0
    assert out["models"] == []


def test_well_known_advertises_public_endpoint():
    """When v2 is enabled, /.well-known/lmrh-config must list /lmrh/public."""
    from pathlib import Path
    src = Path("app/api/lmrh_v2.py").read_text()
    assert '"public": "/lmrh/public"' in src


def test_public_endpoint_route_registered():
    from pathlib import Path
    src = Path("app/api/lmrh_v2.py").read_text()
    assert '@router.get("/lmrh/public")' in src


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 18)
