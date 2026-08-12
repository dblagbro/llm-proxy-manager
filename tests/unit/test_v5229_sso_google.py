"""v5.22.9 — OIDC SSO (Google) endpoint behaviour.

/api/auth/sso/callback mints SESSIONS from an unauthenticated request, so
these tests pin the checks that stop it being an authentication bypass:

  * issuer / audience / expiry / nonce are all validated
  * state is server-side, single-use, and expires (no replay)
  * email_verified=false is refused
  * the domain allow-list is enforced
  * unknown accounts are refused unless auto-provision is explicitly on
  * a provisioned user gets an unusable local password
  * /config leaks nothing beyond a boolean + label
"""
import sys
import time
import types

import pytest

sys.modules.setdefault("litellm", types.ModuleType("litellm"))

from app.api import auth_sso  # noqa: E402


def _jwt(payload: dict) -> str:
    """Build an unsigned JWT — the code parses the payload, not the signature."""
    import base64
    import json

    def seg(d):
        raw = json.dumps(d).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{seg({'alg': 'RS256'})}.{seg(payload)}.sig"


CFG = {
    "enabled": True, "issuer": "https://accounts.google.com",
    "client_id": "cid", "client_secret": "csec",
    "redirect_uri": "https://x/llm-proxy2/api/auth/sso/callback",
    "default_role": "user", "allowed_domains": [], "auto_provision": False,
}


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, results=None):
        self._q = list(results or [])
        self.added = []
        self.commits = 0

    async def execute(self, *_a, **_k):
        return _Result(self._q.pop(0) if self._q else [])

    def add(self, o):
        self.added.append(o)

    async def commit(self):
        self.commits += 1


class _FakeRequest:
    def __init__(self):
        self.headers = {"x-forwarded-prefix": "/llm-proxy2"}
        self.base_url = "https://x/"


class _FakeResponse:
    pass


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    auth_sso._pending.clear()
    auth_sso._discovery_cache.clear()
    monkeypatch.setattr(auth_sso, "_cfg", lambda: dict(CFG))

    async def fake_discover(_issuer):
        return {
            "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token",
        }

    monkeypatch.setattr(auth_sso, "_discover", fake_discover)
    yield
    auth_sso._pending.clear()


def _arm(nonce="N", verifier="V", age=0.0):
    """Put a pending state in place as /start would have."""
    auth_sso._pending["S"] = {
        "nonce": nonce, "verifier": verifier, "created_at": time.time() - age,
    }


def _token_response(monkeypatch, claims, status=200):
    payload = {
        "iss": "https://accounts.google.com", "aud": "cid",
        "exp": time.time() + 600, "nonce": "N", "email_verified": True,
    }
    payload.update(claims)

    class _R:
        status_code = status

        @staticmethod
        def json():
            return {"id_token": _jwt(payload)}

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_k):
            return _R()

    monkeypatch.setattr(auth_sso.httpx, "AsyncClient", lambda *a, **k: _C())


async def _callback(db, **kw):
    return await auth_sso.sso_callback(
        _FakeRequest(), _FakeResponse(),
        code=kw.get("code", "abc"), state=kw.get("state", "S"),
        error=kw.get("error"), db=db,
    )


def _redirect_reason(resp) -> str:
    loc = resp.headers["location"]
    return loc.split("sso_error=")[1] if "sso_error=" in loc else ""


class TestConfigEndpointLeaksNothing:
    @pytest.mark.asyncio
    async def test_only_enabled_and_label(self):
        out = await auth_sso.sso_config()
        assert set(out) == {"enabled", "label"}
        assert out["enabled"] is True
        assert "cid" not in str(out) and "csec" not in str(out)


class TestStateHandling:
    @pytest.mark.asyncio
    async def test_unknown_state_rejected(self):
        r = await _callback(_FakeDB())
        assert _redirect_reason(r) == "expired"

    @pytest.mark.asyncio
    async def test_state_is_single_use(self, monkeypatch):
        _arm()
        _token_response(monkeypatch, {"email": "known@x.com"})
        await _callback(_FakeDB([[types.SimpleNamespace(
            id="u1", username="known", role="admin", email="known@x.com")]]))
        # replaying the same state must not mint a second session
        r2 = await _callback(_FakeDB())
        assert _redirect_reason(r2) == "expired"

    @pytest.mark.asyncio
    async def test_expired_state_swept(self, monkeypatch):
        _arm(age=auth_sso._STATE_TTL_SEC + 60)
        _token_response(monkeypatch, {"email": "known@x.com"})
        r = await _callback(_FakeDB())
        assert _redirect_reason(r) == "expired"

    @pytest.mark.asyncio
    async def test_idp_error_is_reported_not_crashed(self):
        r = await _callback(_FakeDB(), error="access_denied")
        assert _redirect_reason(r) == "denied"


class TestTokenClaimValidation:
    @pytest.mark.asyncio
    async def test_audience_mismatch_rejected(self, monkeypatch):
        _arm()
        _token_response(monkeypatch, {"aud": "someone-else", "email": "a@x.com"})
        assert _redirect_reason(await _callback(_FakeDB())) == "invalid"

    @pytest.mark.asyncio
    async def test_nonce_mismatch_rejected(self, monkeypatch):
        _arm(nonce="EXPECTED")
        _token_response(monkeypatch, {"nonce": "ATTACKER", "email": "a@x.com"})
        assert _redirect_reason(await _callback(_FakeDB())) == "invalid"

    @pytest.mark.asyncio
    async def test_expired_id_token_rejected(self, monkeypatch):
        _arm()
        _token_response(monkeypatch, {"exp": time.time() - 10, "email": "a@x.com"})
        assert _redirect_reason(await _callback(_FakeDB())) == "expired"

    @pytest.mark.asyncio
    async def test_issuer_mismatch_rejected(self, monkeypatch):
        _arm()
        _token_response(monkeypatch, {"iss": "https://evil.example", "email": "a@x.com"})
        assert _redirect_reason(await _callback(_FakeDB())) == "invalid"

    @pytest.mark.asyncio
    async def test_token_endpoint_failure_rejected(self, monkeypatch):
        _arm()
        _token_response(monkeypatch, {"email": "a@x.com"}, status=400)
        assert _redirect_reason(await _callback(_FakeDB())) == "exchange_failed"


class TestEmailPolicy:
    @pytest.mark.asyncio
    async def test_unverified_email_rejected(self, monkeypatch):
        _arm()
        _token_response(monkeypatch, {"email": "a@x.com", "email_verified": False})
        assert _redirect_reason(await _callback(_FakeDB())) == "unverified"

    @pytest.mark.asyncio
    async def test_missing_email_rejected(self, monkeypatch):
        _arm()
        _token_response(monkeypatch, {})
        assert _redirect_reason(await _callback(_FakeDB())) == "no_email"

    @pytest.mark.asyncio
    async def test_domain_allow_list_enforced(self, monkeypatch):
        monkeypatch.setattr(auth_sso, "_cfg",
                            lambda: dict(CFG, allowed_domains=["voipguru.org"]))
        _arm()
        _token_response(monkeypatch, {"email": "someone@gmail.com"})
        assert _redirect_reason(await _callback(_FakeDB())) == "domain"


class TestAccountBinding:
    @pytest.mark.asyncio
    async def test_unknown_account_refused_when_autoprovision_off(self, monkeypatch):
        _arm()
        _token_response(monkeypatch, {"email": "nobody@x.com"})
        db = _FakeDB([[], []])          # no email match, no username match
        assert _redirect_reason(await _callback(db)) == "no_account"
        assert db.added == [], "must not create an account"

    @pytest.mark.asyncio
    async def test_autoprovision_creates_user_with_unusable_password(self, monkeypatch):
        monkeypatch.setattr(auth_sso, "_cfg", lambda: dict(CFG, auto_provision=True))
        monkeypatch.setattr(auth_sso, "create_session",
                            lambda *_a, **_k: _async_value("tok"))
        _arm()
        _token_response(monkeypatch, {"email": "new@x.com"})
        db = _FakeDB([[], []])
        resp = await _callback(db)
        assert "sso_error" not in resp.headers["location"]
        assert len(db.added) == 1
        created = db.added[0]
        assert created.email == "new@x.com"
        assert created.password_hash and len(created.password_hash) > 20

    @pytest.mark.asyncio
    async def test_existing_user_matched_by_email(self, monkeypatch):
        monkeypatch.setattr(auth_sso, "create_session",
                            lambda *_a, **_k: _async_value("tok"))
        _arm()
        _token_response(monkeypatch, {"email": "known@x.com"})
        user = types.SimpleNamespace(id="u1", username="known", role="admin",
                                     email="known@x.com")
        db = _FakeDB([[user]])
        resp = await _callback(db)
        assert "sso_error" not in resp.headers["location"]
        assert db.added == [], "must reuse the existing account"


def _async_value(v):
    async def _c():
        return v
    return _c()


class TestWiring:
    def test_routes_registered(self):
        paths = {r.path for r in auth_sso.router.routes}
        assert {"/api/auth/sso/config", "/api/auth/sso/start",
                "/api/auth/sso/callback"} <= paths

    def test_sso_paths_bypass_the_allowed_paths_gate(self):
        from app.middleware.allowed_paths import _is_bypass_path
        assert _is_bypass_path("/api/auth/sso/start")
        assert _is_bypass_path("/api/auth/sso/callback")

    def test_auto_provision_defaults_off(self):
        from app.config import settings
        assert settings.sso_auto_provision is False, (
            "silent account creation must be opt-in"
        )

    def test_disabled_by_default(self):
        from app.config import settings
        assert settings.sso_enabled is False
