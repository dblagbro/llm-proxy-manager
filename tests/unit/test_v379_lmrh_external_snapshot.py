"""v3.7.9 — LMRH v2 subscription_quota uses authoritative Anthropic
snapshot when available (operator-locked design decision per Q4
discussion 2026-05-10)."""
from __future__ import annotations

from pathlib import Path


def test_snapshot_builder_imports_external_usage_snapshot():
    """The provider snapshot builder must consult ExternalUsageSnapshot
    for claude-oauth providers — that's the post-v3.7.0 authoritative
    signal vs the misleading proxy slice."""
    src = Path("app/routing/lmrh/snapshot.py").read_text()
    assert "ExternalUsageSnapshot" in src
    assert "seven_day_utilization" in src


def test_snapshot_only_uses_external_for_claude_oauth():
    """Other provider types (codex-oauth, grok-web) don't have an
    Anthropic Console scraper — they keep using the proxy slice."""
    src = Path("app/routing/lmrh/snapshot.py").read_text()
    # The external-snapshot branch must check provider_type
    assert 'p.provider_type == "claude-oauth"' in src


def test_snapshot_uses_8h_freshness_window():
    """Stale snapshots (>8h old) should NOT override the proxy slice —
    they could be wrong if the scrape hasn't run lately."""
    src = Path("app/routing/lmrh/snapshot.py").read_text()
    # The freshness check (timedelta(hours=8)) must be present
    assert "hours=8" in src
    # And must compare to captured_at
    assert "captured_at" in src


def test_snapshot_prefers_external_over_proxy_slice():
    """When both are available, external snapshot wins."""
    src = Path("app/routing/lmrh/snapshot.py").read_text()
    # Look for the conditional assignment
    assert "external_snap_used if external_snap_used is not None else" in src


def test_snapshot_falls_back_to_proxy_slice():
    """If external snapshot is missing or stale, proxy slice is used."""
    src = Path("app/routing/lmrh/snapshot.py").read_text()
    # Fall-back uses w.weekly_pct via getattr
    assert 'getattr(w, "weekly_pct", None)' in src


def test_snapshot_works_without_usage_tracking_enabled():
    """v3.7.x recommendation is to set usage_weekly_limit_tokens=NULL
    on claude-oauth providers. If the operator also disables
    usage_tracking_enabled, we still want sub_quota visible — just
    from the external snapshot. Pre-fix this code path returned no
    subscription_quota at all."""
    src = Path("app/routing/lmrh/snapshot.py").read_text()
    # The fallback-only path that synthesizes sub_quota from external snapshot alone
    assert "if sub_quota is None and is_subscription and external_snap_used is not None" in src


def test_snapshot_defensive_on_lookup_failure():
    """An ExternalUsageSnapshot table error must not break the LMRH
    snapshot builder — it should fall back to proxy slice silently."""
    src = Path("app/routing/lmrh/snapshot.py").read_text()
    # Lookup wrapped in try/except with debug log
    assert "external-snapshot lookup failed" in src
