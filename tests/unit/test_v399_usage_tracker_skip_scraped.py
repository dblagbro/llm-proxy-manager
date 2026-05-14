"""v3.9.9 — usage_tracker skips providers with fresh ExternalUsageSnapshot.

After the v3.9.8 quota fix, /api/providers prefers ExternalUsageSnapshot
values when available. Computing ProviderUsageWindow values for those
providers every 60s is wasted DB work — the UI ignores the result.

This test locks the skip logic so it doesn't get re-removed in a refactor.
"""
from __future__ import annotations

from pathlib import Path


def test_usage_tracker_imports_external_usage_snapshot():
    src = Path("app/monitoring/usage_tracker.py").read_text()
    assert "from app.models.db import ExternalUsageSnapshot" in src


def test_usage_tracker_skip_fresh_scraped_providers():
    src = Path("app/monitoring/usage_tracker.py").read_text()
    # The sweep computes a set of provider_ids with fresh snapshots,
    # then early-continues in the per-provider loop.
    assert "fresh_scraped_ids" in src
    assert "fresh_cutoff" in src
    assert "skipped_scraped" in src
    assert "if p.id in fresh_scraped_ids:" in src


def test_freshness_threshold_matches_billing_floor():
    """Skip threshold is 2h to match the default
    ``anthropic_billing_freshness_floor_sec`` (7200s)."""
    src = Path("app/monitoring/usage_tracker.py").read_text()
    assert "hours=2" in src


def test_skipped_count_logged_at_debug():
    src = Path("app/monitoring/usage_tracker.py").read_text()
    assert "usage_tracker.skipped_scraped count=" in src


def test_data_source_field_in_frontend_types():
    src = Path("frontend/src/types/index.ts").read_text()
    assert "usage_data_source" in src
    assert "external_scrape" in src
    assert "internal_window" in src


def test_dashboard_renders_source_label():
    src = Path("frontend/src/pages/DashboardPage.tsx").read_text()
    assert "quotaSourceLabel" in src
    assert "Anthropic Console" in src
    assert "internal counter" in src
    assert "mixed sources" in src
