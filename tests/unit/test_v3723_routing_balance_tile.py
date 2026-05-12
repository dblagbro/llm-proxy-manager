"""v3.7.23 (#255) — Routing balance dashboard tile for claude-oauth.

Source-level checks since the tile lives in the frontend TS bundle and
there is no live React test harness in this repo. The /api/monitoring/
metrics endpoint and providers list already cover the data; the only
new code is the visualization, which we verify by inspecting the
DashboardPage source for the expected behavior.
"""
from __future__ import annotations

from pathlib import Path


def _dashboard_src() -> str:
    return Path("frontend/src/pages/DashboardPage.tsx").read_text()


def test_tile_filters_to_claude_oauth_enabled_providers():
    src = _dashboard_src()
    assert "p.provider_type === 'claude-oauth'" in src
    # Must also gate on enabled — disabled providers should not appear
    assert "p.enabled" in src


def test_tile_hidden_when_under_two_oauth_providers():
    """A 'balance' tile with only one provider is meaningless. Hide it
    when there are 0 or 1 claude-oauth providers."""
    src = _dashboard_src()
    assert "oauthProviders.length < 2" in src


def test_tile_computes_share_from_requests():
    """Share is per-provider requests / total claude-oauth requests."""
    src = _dashboard_src()
    # Anchor on the section's leading comment so the slice covers both
    # the data-shaping block and the JSX body.
    idx = src.index("Routing balance tile for claude-oauth")
    section = src[idx:idx + 6000]
    assert "totalReqs" in section
    assert "requests / totalReqs" in section


def test_tile_surfaces_weekly_utilization():
    """Per-provider weekly utilization is shown alongside share."""
    src = _dashboard_src()
    # Anchor on the section's leading comment so the slice covers both
    # the data-shaping block and the JSX body.
    idx = src.index("Routing balance tile for claude-oauth")
    section = src[idx:idx + 6000]
    assert "usage_weekly_pct" in section
    assert "util" in section


def test_tile_marks_auto_skipped_providers():
    """Auto-skipped providers (auto_skip_until non-null) get a visible
    badge so operators don't think the lopsided traffic is a bug."""
    src = _dashboard_src()
    # Anchor on the section's leading comment so the slice covers both
    # the data-shaping block and the JSX body.
    idx = src.index("Routing balance tile for claude-oauth")
    section = src[idx:idx + 6000]
    assert "auto_skip_until" in section
    assert "auto-skip" in section


def test_tile_handles_zero_traffic_gracefully():
    """When totalReqs is 0, the tile must not divide by zero and should
    show a friendly empty-state message."""
    src = _dashboard_src()
    # Anchor on the section's leading comment so the slice covers both
    # the data-shaping block and the JSX body.
    idx = src.index("Routing balance tile for claude-oauth")
    section = src[idx:idx + 6000]
    # totalReqs > 0 short-circuits the percent calculation
    assert "totalReqs > 0" in section
    # Empty-state copy is present
    assert "No claude-oauth requests in the last 24h" in section


def test_tile_explains_v3720_bucket_filter():
    """Footer copy reminds operators why the share may be heavily skewed
    — it's the BUG-020/v3.7.20 fix, not a routing bug."""
    src = _dashboard_src()
    # Anchor on the section's leading comment so the slice covers both
    # the data-shaping block and the JSX body.
    idx = src.index("Routing balance tile for claude-oauth")
    section = src[idx:idx + 6000]
    assert "v3.7.20" in section
    assert "bucket filter" in section


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 23)
