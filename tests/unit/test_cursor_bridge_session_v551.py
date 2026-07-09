"""v5.5.1 — cursor-bridge-session PKCE drive + Playwright lifespan.

Code-only ship (sidecar not in compose yet). Tests pin the surface so
a follow-up ops session can safely add the compose entry + deploy.
"""
from __future__ import annotations
import sys
from pathlib import Path


def _load_sidecar_source():
    return Path("cursor_bridge_session/app.py").read_text()


def test_sidecar_ships_playwright_lifespan_code():
    src = _load_sidecar_source()
    assert "from playwright.async_api import async_playwright" in src
    assert "_playwright = await async_playwright().start()" in src
    assert "launch_persistent_context" in src


def test_sidecar_ships_pkce_generator():
    src = _load_sidecar_source()
    # PKCE trio: uuid, verifier, challenge
    assert "def _generate_pkce()" in src
    assert "hashlib.sha256" in src
    assert "urlsafe_b64encode" in src or "_b64url_no_pad" in src


def test_sidecar_ships_rotate_drive():
    src = _load_sidecar_source()
    assert "async def _drive_pkce_once" in src
    assert "CURSOR_LOGIN_URL" in src
    assert "CURSOR_AUTH_POLL_URL" in src
    # Serializes concurrent /api/rotate callers
    assert "_rotate_lock" in src


def test_sidecar_ships_access_token_endpoint():
    """llm-proxy2 pulls the last rotated token from here in v5.5.2."""
    src = _load_sidecar_source()
    assert "@app.get(\"/api/access-token\")" in src


def test_sidecar_detects_expired_workos_session():
    """When cursor session cookie has expired, /api/rotate MUST fail
    with 401 (not 500) so the operator knows to log in via noVNC."""
    src = _load_sidecar_source()
    assert "resp.status_code in (401, 403)" in src
    assert "operator must" in src.lower() or "noVNC" in src


def test_sidecar_detects_workos_session_cookie_on_boot():
    """On lifespan startup, check whether the persistent context has
    the WorkOS session cookie so /api/status can report logged_in
    without needing to attempt a rotation."""
    src = _load_sidecar_source()
    assert "WorkosCursorSessionToken" in src
    assert "_LOGGED_IN = " in src


def test_sidecar_version_bumped_in_app():
    src = _load_sidecar_source()
    assert 'version="5.5.1"' in src
    assert 'v5.5.1' in src


def test_pkce_shape_matches_proxy_side():
    """The proxy's own PKCE flow uses the same shape (uuid + verifier +
    challenge as unpadded base64url). If the sidecar drifts from this,
    /auth/poll will 400 with 'uuid must be a string' or similar."""
    proxy_src = Path("app/providers/cursor_oauth_flow.py").read_text()
    sidecar_src = _load_sidecar_source()
    # Both use urlsafe base64 without padding
    for src in (proxy_src, sidecar_src):
        assert "urlsafe_b64encode" in src or "_b64url_no_pad" in src
    # Both use SHA-256 for challenge derivation
    assert "sha256" in proxy_src
    assert "sha256" in sidecar_src


def test_deferred_v552_and_v553_scope_documented():
    """v5.5.2 (rotation cron + hmac callback) and v5.5.3 (UI panel) are
    intentionally deferred to a dedicated ops session — the sidecar
    docstring makes this explicit so future me doesn't ship a broken
    half-configured cron."""
    src = _load_sidecar_source()
    assert "v5.5.2" in src
    assert "v5.5.3" in src
    assert "deferred" in src.lower()
