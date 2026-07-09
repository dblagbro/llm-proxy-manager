"""v5.15.2 (#508) — Frontend Accounts panel.

Bundles with v5.15.1 dispatch flip per operator's 2026-06-30 sign-off.
Renders on the ProviderForm for OAuth-flavored providers with add /
rename / toggle / soft-delete + click-to-reveal on the stored token.

This test file locks the wiring — the panel component exists, it's
imported + mounted in ProviderForm, the API serializer surfaces
``oauth_account_strategy``, and version is bumped.
"""
from __future__ import annotations

from pathlib import Path


# ── (1) Panel component exists + shape ────────────────────────────────


def test_oauth_accounts_panel_module_exists():
    p = Path("frontend/src/components/providers/OAuthAccountsPanel.tsx")
    assert p.exists(), "OAuthAccountsPanel.tsx must exist"


def test_panel_self_gates_on_oauth_provider_type():
    """Component MUST early-return for non-OAuth providers so the mount
    site in ProviderForm doesn't have to know the provider type."""
    src = Path("frontend/src/components/providers/OAuthAccountsPanel.tsx").read_text()
    assert "OAUTH_TYPES" in src
    assert "cursor-oauth" in src
    assert "codex-oauth" in src
    assert "claude-oauth" in src
    assert "if (!isOAuthProvider) return null" in src


def test_panel_covers_the_crud_endpoints():
    src = Path("frontend/src/components/providers/OAuthAccountsPanel.tsx").read_text()
    # Every admin endpoint on the backend has a call site in the panel.
    assert "/api/admin/providers/${provider.id}/oauth-accounts" in src
    # Add + toggle + rename (PATCH) + delete
    assert "api.post" in src
    assert "api.patch" in src
    assert "method: 'DELETE'" in src


def test_panel_covers_ux_niceties():
    """Reveal button, mask helper, and last_used_at humanization all
    ship in v5.15.2. If these get lost in a future refactor, that's
    an operator-visible regression."""
    src = Path("frontend/src/components/providers/OAuthAccountsPanel.tsx").read_text()
    assert "function mask(" in src
    assert "function fmtLastUsed(" in src
    assert "reveal" in src.lower()


# ── (2) Mount site in ProviderForm ───────────────────────────────────


def test_panel_mounted_in_provider_form():
    src = Path("frontend/src/components/providers/ProviderForm.tsx").read_text()
    assert "OAuthAccountsPanel" in src
    assert "from './OAuthAccountsPanel'" in src
    # Mount MUST NOT gate on provider_type — the component self-gates.
    # If the mount site adds a provider_type check, the test still passes;
    # but if it removes the mount entirely, this catches it.
    assert "<OAuthAccountsPanel" in src


# ── (3) API surfaces oauth_account_strategy ──────────────────────────


def test_api_provider_serializer_surfaces_strategy():
    src = Path("app/api/providers.py").read_text()
    assert '"oauth_account_strategy":' in src
    # And uses getattr fallback so a Provider mock without the column
    # doesn't 500 the endpoint.
    assert 'getattr(p, "oauth_account_strategy"' in src


# ── (4) Frontend type gets the field ─────────────────────────────────


def test_frontend_provider_type_has_strategy_field():
    src = Path("frontend/src/types/index.ts").read_text()
    assert "oauth_account_strategy" in src


# ── (5) Version bumped ───────────────────────────────────────────────


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 15, 2), (
        f"expected >= 5.15.2, got {major}.{minor}.{patch}"
    )
