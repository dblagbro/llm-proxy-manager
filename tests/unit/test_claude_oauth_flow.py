"""Unit tests for the browser-initiated Claude Pro Max OAuth flow (v2.7.1)."""
from __future__ import annotations

import sys
import time
import types
from urllib.parse import urlparse, parse_qs

import pytest

# Stub heavy deps before app imports (mirrors test_claude_oauth.py)
_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.providers import claude_oauth_flow as flow
from app.providers.claude_oauth_flow import (
    AUTHORIZE_URL,
    CLIENT_ID,
    DEFAULT_SCOPE,
    REDIRECT_URI,
    OAuthFlowError,
    exchange_code,
    extract_code_from_callback,
    refresh_access_token,
    start_authorize,
    _code_challenge,
    _gen_code_verifier,
    _PENDING,
)


@pytest.fixture(autouse=True)
def _reset_pending():
    _PENDING.clear()
    yield
    _PENDING.clear()


# ── Mock httpx.AsyncClient ──────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status_code: int, data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text or str(data)

    def json(self):
        return self._data


class _FakeClient:
    captured: list[dict] = []
    next_status: int = 200
    next_data: dict = {}
    next_text: str = ""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, data=None, json=None, headers=None):
        # The flow POSTs application/json; older code used form-urlencoded.
        # We capture whichever kwarg was used so tests can assert on it.
        _FakeClient.captured.append({
            "url": url, "data": data, "json": json, "headers": headers,
        })
        return _FakeResp(_FakeClient.next_status, _FakeClient.next_data, _FakeClient.next_text)


@pytest.fixture
def fake_http(monkeypatch):
    import httpx
    _FakeClient.captured = []
    _FakeClient.next_status = 200
    _FakeClient.next_data = {}
    _FakeClient.next_text = ""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return _FakeClient


# ── PKCE helpers ────────────────────────────────────────────────────────────
class TestPKCE:
    def test_verifier_length_within_rfc(self):
        v = _gen_code_verifier()
        assert 43 <= len(v) <= 128

    def test_verifier_uses_url_safe_charset(self):
        import re
        v = _gen_code_verifier()
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", v) is not None

    def test_verifier_uniqueness(self):
        verifiers = {_gen_code_verifier() for _ in range(32)}
        assert len(verifiers) == 32  # no collisions

    def test_code_challenge_deterministic(self):
        v = "fixed-verifier-for-test"
        assert _code_challenge(v) == _code_challenge(v)

    def test_code_challenge_differs_from_verifier(self):
        v = _gen_code_verifier()
        assert _code_challenge(v) != v

    def test_code_challenge_no_padding(self):
        # Base64url with no `=` padding per RFC 7636
        assert "=" not in _code_challenge("some-verifier")


# ── start_authorize ─────────────────────────────────────────────────────────
class TestStartAuthorize:
    def test_returns_state_and_url(self):
        r = start_authorize()
        assert r.state
        assert r.authorize_url.startswith(AUTHORIZE_URL + "?")

    def test_url_contains_required_oauth_params(self):
        r = start_authorize()
        q = parse_qs(urlparse(r.authorize_url).query)
        assert q["response_type"] == ["code"]
        assert q["client_id"] == [CLIENT_ID]
        assert q["redirect_uri"] == [REDIRECT_URI]
        assert q["code_challenge_method"] == ["S256"]
        assert q["state"] == [r.state]
        assert q["code_challenge"][0]  # non-empty
        assert q["scope"] == [DEFAULT_SCOPE]

    def test_custom_scope(self):
        r = start_authorize(scope="user:profile only")
        q = parse_qs(urlparse(r.authorize_url).query)
        assert q["scope"] == ["user:profile only"]

    def test_pending_entry_created(self):
        r = start_authorize()
        assert r.state in _PENDING
        assert _PENDING[r.state].code_verifier

    def test_states_unique(self):
        states = {start_authorize().state for _ in range(10)}
        assert len(states) == 10


# ── extract_code_from_callback ──────────────────────────────────────────────
class TestExtractCode:
    def test_full_url(self):
        code, state = extract_code_from_callback(
            "http://localhost/callback?code=abc123&state=xyz"
        )
        assert code == "abc123"
        assert state == "xyz"

    def test_https_url(self):
        code, state = extract_code_from_callback(
            "https://localhost/callback?code=abc123&state=xyz"
        )
        assert (code, state) == ("abc123", "xyz")

    def test_query_fragment(self):
        code, state = extract_code_from_callback("code=abc123&state=xyz")
        assert (code, state) == ("abc123", "xyz")

    def test_query_fragment_leading_question_mark(self):
        code, state = extract_code_from_callback("?code=abc123&state=xyz")
        assert (code, state) == ("abc123", "xyz")

    def test_bare_code(self):
        code, state = extract_code_from_callback("abc123")
        assert code == "abc123"
        assert state is None

    def test_code_hash_state(self):
        """The 'CODE#STATE' format is what Anthropic's success page shows."""
        code, state = extract_code_from_callback("abc123#xyz789")
        assert code == "abc123"
        assert state == "xyz789"

    def test_code_hash_state_whitespace_trimmed(self):
        code, state = extract_code_from_callback("  abc123#xyz789\n")
        assert code == "abc123"
        assert state == "xyz789"

    def test_code_hash_state_truncated_rejected(self):
        with pytest.raises(ValueError, match="truncated"):
            extract_code_from_callback("abc123#")
        with pytest.raises(ValueError, match="truncated"):
            extract_code_from_callback("#xyz789")

    def test_strips_whitespace(self):
        code, state = extract_code_from_callback("   abc123\n")
        assert code == "abc123"

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            extract_code_from_callback("")
        with pytest.raises(ValueError):
            extract_code_from_callback("   ")

    def test_url_without_code_rejected(self):
        with pytest.raises(ValueError):
            extract_code_from_callback("http://localhost/callback?state=xyz")

    def test_fragment_without_code_rejected(self):
        with pytest.raises(ValueError):
            extract_code_from_callback("state=xyz")


# ── exchange_code ───────────────────────────────────────────────────────────
class TestExchangeCode:
    @pytest.mark.asyncio
    async def test_success_returns_tokens(self, fake_http):
        r = start_authorize()
        fake_http.next_status = 200
        fake_http.next_data = {
            "access_token": "sk-ant-oat01-new",
            "refresh_token": "r3fr3sh",
            "expires_in": 3600,
        }
        result = await exchange_code(r.state, "the-code")
        assert result.access_token == "sk-ant-oat01-new"
        assert result.refresh_token == "r3fr3sh"
        assert result.expires_at is not None
        assert abs(result.expires_at - (time.time() + 3600)) < 5

    @pytest.mark.asyncio
    async def test_sends_correct_form(self, fake_http):
        r = start_authorize()
        pending = _PENDING[r.state]
        fake_http.next_data = {"access_token": "sk-ant-oat01-x"}
        await exchange_code(r.state, "the-code")
        sent = fake_http.captured[0]
        assert sent["url"] == flow.TOKEN_URL
        body = sent["json"]  # POSTed as JSON, not form-urlencoded
        assert body["grant_type"] == "authorization_code"
        assert body["client_id"] == CLIENT_ID
        assert body["code"] == "the-code"
        assert body["redirect_uri"] == REDIRECT_URI
        assert body["code_verifier"] == pending.code_verifier
        # Anthropic's /v1/oauth/token requires state in the form (non-standard)
        assert body["state"] == r.state

    @pytest.mark.asyncio
    async def test_pending_cleared_on_success(self, fake_http):
        r = start_authorize()
        fake_http.next_data = {"access_token": "sk-ant-oat01-x"}
        await exchange_code(r.state, "the-code")
        assert r.state not in _PENDING

    @pytest.mark.asyncio
    async def test_unknown_state_rejected(self, fake_http):
        with pytest.raises(OAuthFlowError, match="Unknown or expired state"):
            await exchange_code("not-a-real-state", "the-code")

    @pytest.mark.asyncio
    async def test_expected_state_mismatch_rejected(self, fake_http):
        r = start_authorize()
        with pytest.raises(OAuthFlowError, match="state mismatch"):
            await exchange_code(r.state, "the-code", expected_state="different")
        # Pending was NOT consumed
        assert r.state in _PENDING

    @pytest.mark.asyncio
    async def test_expected_state_match_ok(self, fake_http):
        r = start_authorize()
        fake_http.next_data = {"access_token": "sk-ant-oat01-x"}
        result = await exchange_code(r.state, "the-code", expected_state=r.state)
        assert result.access_token == "sk-ant-oat01-x"

    @pytest.mark.asyncio
    async def test_http_error_reprises_pending(self, fake_http):
        """On upstream 4xx/5xx we put the pending back so retries work."""
        r = start_authorize()
        fake_http.next_status = 400
        fake_http.next_data = {"error": "invalid_grant"}
        fake_http.next_text = '{"error":"invalid_grant"}'
        with pytest.raises(OAuthFlowError, match="Token exchange failed"):
            await exchange_code(r.state, "bad-code")
        # Pending is restored so admin can retry
        assert r.state in _PENDING

    @pytest.mark.asyncio
    async def test_missing_access_token_rejected(self, fake_http):
        r = start_authorize()
        fake_http.next_status = 200
        fake_http.next_data = {"refresh_token": "r3fr3sh"}  # no access_token
        with pytest.raises(OAuthFlowError, match="no access_token"):
            await exchange_code(r.state, "the-code")

    @pytest.mark.asyncio
    async def test_refresh_token_optional(self, fake_http):
        r = start_authorize()
        fake_http.next_data = {"access_token": "sk-ant-oat01-x"}  # no refresh
        result = await exchange_code(r.state, "the-code")
        assert result.refresh_token is None


# ── refresh_access_token ────────────────────────────────────────────────────
class TestRefresh:
    @pytest.mark.asyncio
    async def test_success(self, fake_http):
        fake_http.next_data = {
            "access_token": "sk-ant-oat01-refreshed",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
        }
        result = await refresh_access_token("old-refresh")
        assert result.access_token == "sk-ant-oat01-refreshed"
        assert result.refresh_token == "new-refresh"
        assert result.expires_at is not None

    @pytest.mark.asyncio
    async def test_sends_refresh_grant(self, fake_http):
        fake_http.next_data = {"access_token": "sk-ant-oat01-x"}
        await refresh_access_token("my-refresh")
        sent = fake_http.captured[0]
        body = sent["json"]
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "my-refresh"
        assert body["client_id"] == CLIENT_ID

    @pytest.mark.asyncio
    async def test_keeps_old_refresh_when_not_rotated(self, fake_http):
        """Some OAuth servers don't rotate refresh tokens on each use."""
        fake_http.next_data = {"access_token": "sk-ant-oat01-x"}  # no refresh_token
        result = await refresh_access_token("my-refresh")
        assert result.refresh_token == "my-refresh"

    @pytest.mark.asyncio
    async def test_http_error(self, fake_http):
        fake_http.next_status = 401
        fake_http.next_text = "unauthorized"
        with pytest.raises(OAuthFlowError, match="Refresh failed"):
            await refresh_access_token("dead-refresh")


# ── TTL sweep ───────────────────────────────────────────────────────────────
class TestTTLSweep:
    def test_old_entries_swept(self):
        r = start_authorize()
        # Manually age the entry past the TTL
        _PENDING[r.state].created_at = time.time() - (flow.PENDING_TTL_SEC + 10)
        flow._sweep_pending()
        assert r.state not in _PENDING

    def test_fresh_entries_kept(self):
        r = start_authorize()
        flow._sweep_pending()
        assert r.state in _PENDING

    def test_sweep_runs_on_start_authorize(self):
        r1 = start_authorize()
        _PENDING[r1.state].created_at = time.time() - (flow.PENDING_TTL_SEC + 10)
        # New start_authorize should evict the stale entry as a side effect
        start_authorize()
        assert r1.state not in _PENDING
