"""v4.4.31 — Cursor OAuth onboarding (subscription provider via the
``cursor-bridge`` sidecar).

Tests the cursor_oauth + cursor_oauth_flow modules' shape compatibility
with the existing providers_oauth shared machinery, the credential
parser's defensive branches, and the deep-link exchange path against a
mocked sidecar response.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest


# ── Source guards ───────────────────────────────────────────────────


def test_cursor_oauth_spec_registered_in_providers_oauth():
    """The shared dispatch machinery in providers_oauth needs a
    CURSOR_OAUTH_SPEC entry so the /cursor-oauth/{authorize,exchange,
    rotate} endpoints can reuse _do_authorize / _do_exchange_create /
    _do_rotate without per-vendor branches."""
    src = Path("app/api/providers_oauth.py").read_text()
    assert "CURSOR_OAUTH_SPEC" in src
    assert 'provider_type="cursor-oauth"' in src
    assert "app.providers.cursor_oauth_flow" in src


def test_cursor_oauth_subscription_tier_listed():
    """cursor-oauth providers must be in SUBSCRIPTION_TIER_PROVIDER_TYPES
    so cost accounting records $0 on the api_key total and surfaces the
    rated estimate as quota_usd (same shape as claude-oauth / codex /
    grok-web)."""
    src = Path("app/monitoring/helpers.py").read_text()
    idx = src.index("SUBSCRIPTION_TIER_PROVIDER_TYPES")
    block = src[idx:idx + 1500]
    assert '"cursor-oauth"' in block


def test_three_endpoints_present():
    src = Path("app/api/providers_oauth.py").read_text()
    for path in (
        "/cursor-oauth/authorize",
        "/cursor-oauth/exchange",
        "/cursor-oauth-rotate",
    ):
        assert path in src, f"missing endpoint {path}"


def test_frontend_oauth_flavor_entry_present():
    src = Path("frontend/src/components/providers/ProviderForm.tsx").read_text()
    assert "'cursor-oauth':" in src or '"cursor-oauth":' in src
    assert "providersApi.cursorOauthAuthorize" in src
    assert "providersApi.cursorOauthExchange" in src
    assert "providersApi.cursorOauthRotate" in src


def test_frontend_provider_type_listed():
    src = Path("frontend/src/types/index.ts").read_text()
    assert "'cursor-oauth'" in src


# ── cursor_oauth.py — credential parser ─────────────────────────────


def test_parse_credentials_accepts_bare_cursor_cookie():
    from app.providers.cursor_oauth import parse_credentials
    raw = "user_01ABCDEFGHJKMNPQRSTV01ABCDEFGH::eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.fake.signature"
    creds = parse_credentials(raw)
    assert creds.access_token == raw
    assert creds.refresh_token is None  # cursor doesn't issue one


def test_parse_credentials_accepts_json_with_access_token():
    from app.providers.cursor_oauth import parse_credentials
    import json
    blob = json.dumps({"access_token": "user_xxx::eyJ.A.B"})
    creds = parse_credentials(blob)
    assert creds.access_token == "user_xxx::eyJ.A.B"


def test_parse_credentials_accepts_sidecar_response_shape():
    """The sidecar returns ``{"accessToken": "user_..."}`` — accept it
    verbatim so an operator can paste the sidecar's response directly."""
    from app.providers.cursor_oauth import parse_credentials
    import json
    blob = json.dumps({"accessToken": "user_xxx::eyJ.A.B"})
    creds = parse_credentials(blob)
    assert creds.access_token == "user_xxx::eyJ.A.B"


def test_parse_credentials_rejects_empty():
    from app.providers.cursor_oauth import parse_credentials, CredentialParseError
    with pytest.raises(CredentialParseError):
        parse_credentials("")
    with pytest.raises(CredentialParseError):
        parse_credentials("   ")


def test_parse_credentials_rejects_unrecognized_string():
    from app.providers.cursor_oauth import parse_credentials, CredentialParseError
    with pytest.raises(CredentialParseError):
        parse_credentials("not-a-cursor-cookie")


def test_parse_credentials_rejects_malformed_json():
    from app.providers.cursor_oauth import parse_credentials, CredentialParseError
    with pytest.raises(CredentialParseError):
        parse_credentials('{"access_token":}')


def test_looks_like_cursor_token_branches():
    from app.providers.cursor_oauth import looks_like_cursor_token
    assert looks_like_cursor_token("user_xxx::eyJ.A.B") is True
    assert looks_like_cursor_token("eyJ.A.B") is True  # bare JWT
    assert looks_like_cursor_token("sk-ant-oat-blah") is False
    assert looks_like_cursor_token("") is False


# ── cursor_oauth_flow.py — start_authorize + callback parse ─────────


def test_start_authorize_returns_state_and_dashboard_url():
    from app.providers.cursor_oauth_flow import start_authorize, CURSOR_SIGNIN_URL
    start = start_authorize()
    assert start.state and len(start.state) >= 20  # randomness
    assert start.authorize_url == CURSOR_SIGNIN_URL


def test_start_authorize_state_is_unique():
    from app.providers.cursor_oauth_flow import start_authorize
    a = start_authorize().state
    b = start_authorize().state
    assert a != b


def test_extract_code_strips_known_prefixes():
    from app.providers.cursor_oauth_flow import extract_code_from_callback
    cookie = "user_xxx%3A%3AeyJ.A.B"
    for prefix in ("WorkosCursorSessionToken=", "Cookie: WorkosCursorSessionToken="):
        code, state = extract_code_from_callback(prefix + cookie)
        assert code == cookie
        assert state is None  # cursor doesn't carry state in callback


def test_extract_code_drops_trailing_other_cookies():
    """Operator might paste the full Cookie header — keep only the
    first cookie's value."""
    from app.providers.cursor_oauth_flow import extract_code_from_callback
    code, _ = extract_code_from_callback("user_xxx%3A%3AeyJ.A.B; other_cookie=foo")
    assert code == "user_xxx%3A%3AeyJ.A.B"


def test_extract_code_rejects_empty():
    from app.providers.cursor_oauth_flow import extract_code_from_callback
    with pytest.raises(ValueError):
        extract_code_from_callback("")


def test_extract_code_rejects_non_cookie_shape():
    """Catch obvious paste-the-wrong-thing cases (e.g. cookie name
    only, or a JSON blob) early with a readable error."""
    from app.providers.cursor_oauth_flow import extract_code_from_callback
    with pytest.raises(ValueError):
        extract_code_from_callback("WorkosCursorSessionToken")


# ── exchange_code — mocked sidecar response ─────────────────────────


@pytest.mark.asyncio
async def test_exchange_code_calls_sidecar_and_returns_access_token():
    """Drive the full happy path with a mocked sidecar response. The
    sidecar returns ``{"accessToken": "user_<id>::<JWT>"}`` — exchange
    should pass it through as ExchangeResult.access_token."""
    from app.providers.cursor_oauth_flow import (
        start_authorize, exchange_code,
    )

    start = start_authorize()
    fake_jwt = "user_01ABCDEFGH::eyJhbGciOiJIUzI1NiI.fake.signature"

    # Mock the httpx response
    class FakeResp:
        status_code = 200
        text = '{"accessToken":"' + fake_jwt + '"}'
        def json(self): return {"accessToken": fake_jwt}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, headers=None):
            assert "/cursor/loginDeepControl" in url
            assert headers["Authorization"].startswith("Bearer ")
            return FakeResp()

    with patch("app.providers.cursor_oauth_flow.httpx.AsyncClient", FakeClient):
        result = await exchange_code(start.state, "user_xxx%3A%3AeyJ.A.B")
    assert result.access_token == fake_jwt
    assert result.refresh_token is None
    assert result.expires_at is None


@pytest.mark.asyncio
async def test_exchange_code_rejects_unknown_state():
    """Stale modal → state not in _PENDING → OAuthFlowError, NOT a
    silent success that would store no credentials."""
    from app.providers.cursor_oauth_flow import exchange_code, OAuthFlowError
    with pytest.raises(OAuthFlowError):
        await exchange_code("never-issued-state", "user_xxx::eyJ.A.B")


@pytest.mark.asyncio
async def test_exchange_code_propagates_sidecar_non_200():
    """Sidecar returns HTTP 401/500 → OAuthFlowError with the body
    preview so the operator can debug from the error message alone."""
    from app.providers.cursor_oauth_flow import (
        start_authorize, exchange_code, OAuthFlowError,
    )
    start = start_authorize()

    class FakeResp:
        status_code = 401
        text = '{"error":"cursor_session_expired"}'
        def json(self): return {"error": "cursor_session_expired"}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, headers=None):
            return FakeResp()

    with patch("app.providers.cursor_oauth_flow.httpx.AsyncClient", FakeClient):
        with pytest.raises(OAuthFlowError) as ei:
            await exchange_code(start.state, "user_xxx%3A%3AeyJ.A.B")
        assert "401" in str(ei.value)


@pytest.mark.asyncio
async def test_refresh_access_token_is_not_supported_in_v1():
    """v1 doesn't automate refresh — the rotate endpoint is the
    operator path. Background refresh workers must hit this and stop
    cleanly rather than silently no-op."""
    from app.providers.cursor_oauth_flow import (
        refresh_access_token, OAuthFlowError,
    )
    with pytest.raises(OAuthFlowError) as ei:
        await refresh_access_token("anything")
    assert "rotate" in str(ei.value).lower()


# ── State expiry sweep ─────────────────────────────────────────────


def test_pending_state_sweep_drops_old_entries():
    """The in-memory pending dict must clean itself up so abandoned
    flows don't accumulate."""
    from app.providers.cursor_oauth_flow import (
        start_authorize, _PENDING, _sweep_pending, _STATE_TTL_SEC,
    )
    start = start_authorize()
    assert start.state in _PENDING
    # Time-travel: pretend the entry was created _STATE_TTL_SEC + 1 ago
    _PENDING[start.state].created_at = time.time() - _STATE_TTL_SEC - 1
    _sweep_pending()
    assert start.state not in _PENDING
