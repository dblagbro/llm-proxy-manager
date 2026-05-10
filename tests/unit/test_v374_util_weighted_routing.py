"""v3.7.4 — utilization-weighted reorder for claude-oauth providers."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.routing.external_rotation import (
    _utilization_bucket,
    reorder_claude_oauth_by_utilization,
)


# ── _utilization_bucket ────────────────────────────────────────────


def test_bucket_zero_for_low_util():
    assert _utilization_bucket(0.0) == 0
    assert _utilization_bucket(10.0) == 0
    assert _utilization_bucket(24.9) == 0


def test_bucket_one_at_25_threshold():
    assert _utilization_bucket(25.0) == 1
    assert _utilization_bucket(49.9) == 1


def test_bucket_two_at_50():
    assert _utilization_bucket(50.0) == 2
    assert _utilization_bucket(74.9) == 2


def test_bucket_three_at_75():
    assert _utilization_bucket(75.0) == 3
    assert _utilization_bucket(99.9) == 3


def test_bucket_four_at_100():
    assert _utilization_bucket(100.0) == 4


def test_bucket_none_sorts_last():
    """No snapshot data → high bucket so the provider with data wins."""
    assert _utilization_bucket(None) > 100


def test_bucket_size_configurable():
    """Custom bucket size respected."""
    # 10pp buckets — Gmail @ 60% and VG @ 50% in different buckets
    assert _utilization_bucket(60.0, bucket_size_pct=10.0) == 6
    assert _utilization_bucket(50.0, bucket_size_pct=10.0) == 5


def test_bucket_handles_negative():
    """Defensive: never return a negative bucket."""
    assert _utilization_bucket(-5.0) == 0


# ── reorder_claude_oauth_by_utilization — the scenario ────────────


def _provider(*, id, name, provider_type="claude-oauth", priority=4):
    p = MagicMock()
    p.id = id
    p.name = name
    p.provider_type = provider_type
    p.priority = priority
    return p


def test_operator_scenario_swaps_gmail_vg():
    """The exact scenario the operator surfaced:
    Gmail priority=4 at 100% util, VG priority=5 at 24% util.
    Reorder should put VG in Gmail's slot."""
    gmail = _provider(id="gmail", name="Gmail", priority=4)
    vg = _provider(id="vg", name="VG", priority=5)
    util_map = {"gmail": 100.0, "vg": 24.0}
    result = reorder_claude_oauth_by_utilization([gmail, vg], util_map)
    # VG (24% → bucket 0) should now be first, Gmail (100% → bucket 4) last
    assert [p.name for p in result] == ["VG", "Gmail"]


def test_no_reorder_when_same_bucket():
    """If both providers are in the same util bucket (e.g. both at
    30%), operator priority wins as the tie-breaker."""
    a = _provider(id="a", name="A", priority=4)
    b = _provider(id="b", name="B", priority=5)
    util_map = {"a": 30.0, "b": 35.0}  # both in bucket 1
    result = reorder_claude_oauth_by_utilization([a, b], util_map)
    # A keeps its priority=4 lead
    assert [p.name for p in result] == ["A", "B"]


def test_non_claude_oauth_position_preserved():
    """Per-call providers (Vertex, OpenRouter) must keep their
    operator-set slot. Only claude-oauth re-orders within itself."""
    # Layout: Gmail (oauth p=4), Vertex (per-call p=5), VG (oauth p=6)
    gmail = _provider(id="gmail", name="Gmail", priority=4)
    vertex = _provider(id="vertex", name="Vertex", provider_type="compatible", priority=5)
    vg = _provider(id="vg", name="VG", priority=6)
    util_map = {"gmail": 100.0, "vg": 24.0}
    result = reorder_claude_oauth_by_utilization([gmail, vertex, vg], util_map)
    # Expected: VG takes Gmail's slot, Vertex unchanged in middle, Gmail at end
    # Slots: [oauth, non-oauth, oauth] → after reorder among oauth slots: [VG, Vertex, Gmail]
    assert [p.name for p in result] == ["VG", "Vertex", "Gmail"]


def test_single_oauth_no_change():
    """Only one claude-oauth provider → no reorder possible."""
    a = _provider(id="a", name="A")
    b = _provider(id="b", name="B", provider_type="compatible")
    result = reorder_claude_oauth_by_utilization([a, b], {"a": 100.0})
    assert [p.name for p in result] == ["A", "B"]


def test_no_oauth_no_change():
    a = _provider(id="a", name="A", provider_type="compatible")
    b = _provider(id="b", name="B", provider_type="openrouter")
    result = reorder_claude_oauth_by_utilization([a, b], {})
    assert [p.name for p in result] == ["A", "B"]


def test_no_util_data_leaves_priority_order():
    """If no snapshot data exists, both providers map to the same
    bucket (no-data), and operator priority wins."""
    gmail = _provider(id="gmail", name="Gmail", priority=4)
    vg = _provider(id="vg", name="VG", priority=5)
    result = reorder_claude_oauth_by_utilization([gmail, vg], {})
    assert [p.name for p in result] == ["Gmail", "VG"]


def test_provider_with_data_beats_provider_without():
    """If A has util data (low) and B doesn't, A wins because B's
    'no data' bucket is worse than any data bucket."""
    a = _provider(id="a", name="A", priority=5)  # lower priority
    b = _provider(id="b", name="B", priority=4)  # higher priority (lower number)
    # A has data showing 10%; B has no data
    result = reorder_claude_oauth_by_utilization([a, b], {"a": 10.0})
    assert [p.name for p in result] == ["A", "B"]


def test_already_correct_order_passes_through():
    """If the list is already in correct order, we don't waste a copy."""
    vg = _provider(id="vg", name="VG", priority=4)
    gmail = _provider(id="gmail", name="Gmail", priority=5)
    util_map = {"vg": 24.0, "gmail": 100.0}
    in_list = [vg, gmail]
    result = reorder_claude_oauth_by_utilization(in_list, util_map)
    # Same identity (or at least same content) — no swap needed
    assert [p.name for p in result] == ["VG", "Gmail"]


def test_bucket_size_pct_param_threads_through():
    """The bucket_size_pct argument changes reorder behavior."""
    # Gmail at 30, VG at 25. Default 25pp bucket: both in bucket 1
    # (Gmail at 30 → 1, VG at 25 → 1) → tie → priority wins (Gmail p=4)
    # 5pp bucket: Gmail bucket=6, VG bucket=5 → VG wins
    gmail = _provider(id="gmail", name="Gmail", priority=4)
    vg = _provider(id="vg", name="VG", priority=5)
    util_map = {"gmail": 30.0, "vg": 25.0}
    default_order = reorder_claude_oauth_by_utilization([gmail, vg], util_map)
    fine_order = reorder_claude_oauth_by_utilization(
        [gmail, vg], util_map, bucket_size_pct=5.0,
    )
    assert [p.name for p in default_order] == ["Gmail", "VG"]
    assert [p.name for p in fine_order] == ["VG", "Gmail"]


# ── Router integration regression ────────────────────────────────


def test_router_calls_reorder_after_skip_filter():
    """The router must call reorder_claude_oauth_by_utilization AFTER
    is_currently_at_capacity filter — so providers that are entirely
    skipped don't even enter the reorder."""
    from pathlib import Path
    src = Path("app/routing/router.py").read_text()
    # Both functions referenced
    assert "is_currently_at_capacity" in src
    assert "reorder_claude_oauth_by_utilization" in src
    # The filter-out line is before the reorder call
    filter_idx = src.index("p for p in providers if not is_currently_at_capacity")
    reorder_idx = src.index("reorder_claude_oauth_by_utilization(")
    assert filter_idx < reorder_idx


def test_setting_for_bucket_size_exists():
    from app.config import settings
    assert hasattr(settings, "external_rotation_util_bucket_pct")
    assert 0 < settings.external_rotation_util_bucket_pct <= 100
