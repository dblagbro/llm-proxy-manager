"""v3.7.6 — provider-list effective-preference badge regression checks."""
from __future__ import annotations

from pathlib import Path


def test_providers_page_renders_auto_skip_badge():
    """Visual badge when auto_skip_until is active. Title attr shows reason."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "auto-skipped" in src
    assert "auto_skip_until" in src
    # Comparison must use Date().getTime() > Date.now()
    assert "new Date(p.auto_skip_until).getTime() > Date.now()" in src


def test_providers_page_renders_preferred_badge():
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "preferred" in src.lower()
    # Computed from claudeOauthPreferred string
    assert "claudeOauthPreferred" in src


def test_preferred_computation_uses_snapshots_api():
    """The list page must read latest snapshots to rank claude-oauth
    providers, not rely on stored Provider.priority."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "listSnapshots" in src
    assert "seven_day_utilization" in src
    # Sorts by utilization ascending
    assert "claudeOauthUtilMap[a.id]" in src or ".sort(" in src


def test_preferred_skips_at_capacity():
    """A provider currently auto-skipped must NOT win the 'preferred' tag."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    # Filter expression excludes auto_skip_until in the future
    assert "auto_skip_until" in src
    # The candidate filter includes a check for !active-skip
    filter_block = src[src.index("candidates ="):src.index("candidates =") + 600]
    assert "auto_skip_until" in filter_block


def test_preferred_only_runs_with_2plus_oauth_providers():
    """No 'preferred' tag when there's only one claude-oauth provider —
    nothing to rank against."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "claudeOauthIds.length >= 2" in src
    assert "claudeOauthIds.length < 2" in src


def test_provider_list_query_keyed_by_provider_ids():
    """The query must re-fire when the set of claude-oauth providers
    changes (operator adds/removes one)."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "claude-oauth-snapshots" in src
    assert "claudeOauthIdsKey" in src
