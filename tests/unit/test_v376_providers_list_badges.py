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
    """No 'preferred' tag when there's only one subscription provider of
    a given type — nothing to rank against. v4.4.41 generalized the
    variable name from claudeOauthIds to subscriptionIds (covers both
    claude-oauth AND cursor-oauth in one pass) so this asserts the
    new shape."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "subscriptionIds.length >= 2" in src
    # v4.4.41: the per-type < 2 guard now lives inside ``pickPerType``;
    # it reads ``sameType.length < 2`` rather than ``claudeOauthIds.length < 2``.
    assert "sameType.length < 2" in src


def test_provider_list_query_keyed_by_provider_ids():
    """The query must re-fire when the set of subscription-tier providers
    changes (operator adds/removes one). v4.4.41 renamed the query key
    + the deps key to cover both claude-oauth and cursor-oauth in one pass."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "subscription-snapshots" in src
    assert "subscriptionIdsKey" in src
