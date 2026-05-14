"""v3.7.29 (#252 phase 2) — manual override UI banner + 🔒 badge.

Phase 2 wires the operator-facing surface for the manual-override
escape hatch shipped in v3.7.28 (Phase 1):

- ``ManualOverrideBanner`` — top-of-page banner rendered globally in
  Layout.tsx; auto-hides when no providers are locked; offers a
  bulk "Release all to AI control" action.
- 🔒 badge on the provider list (ProvidersPage) AND the dashboard
  provider-status strip (DashboardPage).

Source-level checks since the components live in TS and there's no
live React harness here.
"""
from __future__ import annotations

from pathlib import Path


# ── Banner component ──────────────────────────────────────────────


def test_banner_component_exists():
    p = Path("frontend/src/components/layout/ManualOverrideBanner.tsx")
    assert p.exists(), "ManualOverrideBanner.tsx must exist"


def test_banner_auto_hides_when_no_locks():
    """Banner returns null when no provider has manual_override_active.
    Don't show a banner that says '0 providers locked'."""
    src = Path("frontend/src/components/layout/ManualOverrideBanner.tsx").read_text()
    assert "if (locked.length === 0) return null" in src


def test_banner_filters_on_manual_override_active():
    src = Path("frontend/src/components/layout/ManualOverrideBanner.tsx").read_text()
    assert "p.manual_override_active" in src


def test_banner_calls_release_endpoint():
    src = Path("frontend/src/components/layout/ManualOverrideBanner.tsx").read_text()
    assert "providersApi.releaseManualOverrides" in src


def test_banner_invalidates_providers_query_on_success():
    """After release succeeds, the providers query must be invalidated
    so the banner + all provider rows re-render without the locked state."""
    src = Path("frontend/src/components/layout/ManualOverrideBanner.tsx").read_text()
    assert "invalidateQueries" in src
    assert "'providers'" in src


def test_banner_requires_confirmation_before_release():
    """Release is destructive (releases ALL locks across the cluster).
    A two-step click pattern prevents accidental clicks.

    v3.8.6 — the confirmation prompt now spells out the action ('Release
    locks & re-enable N provider(s)?') instead of the generic 'Confirm?'
    that left operators unsure of what would happen."""
    src = Path("frontend/src/components/layout/ManualOverrideBanner.tsx").read_text()
    assert "confirming" in src
    # New explicit confirmation phrasing
    assert "Release locks &amp; re-enable" in src


def test_banner_expandable_detail_view():
    """Click 'View details' to expand a list of currently-locked
    providers (so operator can see WHICH ones before clicking
    release)."""
    src = Path("frontend/src/components/layout/ManualOverrideBanner.tsx").read_text()
    assert "expanded" in src
    assert "View details" in src
    assert "Hide details" in src


# ── Banner is mounted in Layout ───────────────────────────────────


def test_banner_mounted_in_layout():
    src = Path("frontend/src/components/layout/Layout.tsx").read_text()
    assert "import { ManualOverrideBanner }" in src
    assert "<ManualOverrideBanner />" in src


def test_banner_renders_inside_main_column_above_outlet():
    """Banner must render inside the main column (next to TopBar) so
    it sits ABOVE the page content but below the top bar — not below
    the Outlet (which would be inside the page) or outside the column
    (which would be next to the sidebar)."""
    src = Path("frontend/src/components/layout/Layout.tsx").read_text()
    topbar = src.index("<TopBar />")
    banner = src.index("<ManualOverrideBanner />")
    outlet = src.index("<Outlet />")
    assert topbar < banner < outlet


# ── 🔒 badge in ProvidersPage ─────────────────────────────────────


def test_providers_page_renders_lock_badge():
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "p.manual_override_active" in src
    assert "🔒" in src


def test_providers_page_badge_has_tooltip():
    """The badge title attribute explains what manual-override means
    so the operator doesn't have to remember."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    idx = src.index("p.manual_override_active")
    section = src[idx:idx + 1200]
    assert "title=" in section
    assert "Manual override" in section


# ── 🔒 badge in DashboardPage ProviderStatusRow ───────────────────


def test_dashboard_provider_status_row_renders_lock_badge():
    src = Path("frontend/src/pages/DashboardPage.tsx").read_text()
    idx = src.index("function ProviderStatusRow")
    body = src[idx:idx + 2500]
    assert "provider.manual_override_active" in body
    assert "🔒" in body


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 29)
