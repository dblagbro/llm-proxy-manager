"""v5.22.7 — password reset: admin-initiated (A) + self-service email (B).

Security-critical, because /api/auth/password-reset/* is the only
UNAUTHENTICATED write surface on the service. The tests below pin the
properties that make it safe rather than just the happy path:

  * anti-enumeration — identical response for unknown / no-email / real account
  * tokens stored only as SHA-256, never in the clear
  * single-use, expiring, and voided by any other password change
  * rate limited per IP and per account
  * mail failures never surface to the caller (that would leak existence)
"""
import sys
import time
import types

import pytest

sys.modules.setdefault("litellm", types.ModuleType("litellm"))

from app.api import auth as auth_api  # noqa: E402

# ── fakes ────────────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeDB:
    """Returns queued results in order; records added rows and commits."""

    def __init__(self, results):
        self._queue = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, *_a, **_k):
        return _Result(self._queue.pop(0) if self._queue else [])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


class _FakeUser:
    def __init__(self, uid="u1", username="dblagbro", email="dblagbro@voipguru.org"):
        self.id = uid
        self.username = username
        self.email = email
        self.password_hash = "old-hash"
        self.deleted_at = None
        self.last_user_edit_at = None


class _FakeToken:
    def __init__(self, user_id="u1", token_hash="", expires_in=1800, used_at=None):
        self.user_id = user_id
        self.token_hash = token_hash
        self.created_at = time.time()
        self.expires_at = time.time() + expires_in
        self.used_at = used_at


class _FakeRequest:
    def __init__(self, ip="203.0.113.9"):
        self.client = types.SimpleNamespace(host=ip)
        self.base_url = "https://www.voipguru.org/"
        self.headers = {}


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    auth_api._reset_attempts.clear()
    yield
    auth_api._reset_attempts.clear()


@pytest.fixture
def no_mail(monkeypatch):
    """Capture outbound mail instead of sending it."""
    sent = []

    async def fake_send(to, subject, html):
        sent.append({"to": to, "subject": subject, "html": html})
        return True

    monkeypatch.setattr("app.utils.mailer.send_email_async", fake_send)
    return sent


# ── token handling ───────────────────────────────────────────────────────────

class TestTokenHashing:
    def test_hash_is_sha256_hex(self):
        h = auth_api._hash_reset_token("abc")
        assert len(h) == 64 and int(h, 16) >= 0

    def test_raw_token_is_not_recoverable_from_hash(self):
        assert "supersecret" not in auth_api._hash_reset_token("supersecret")

    def test_hash_is_stable_and_distinct(self):
        assert auth_api._hash_reset_token("a") == auth_api._hash_reset_token("a")
        assert auth_api._hash_reset_token("a") != auth_api._hash_reset_token("b")


class TestRateLimiter:
    def test_blocks_past_the_limit(self):
        assert all(auth_api._reset_rate_ok("k", 3) for _ in range(3))
        assert auth_api._reset_rate_ok("k", 3) is False

    def test_buckets_are_independent(self):
        for _ in range(3):
            auth_api._reset_rate_ok("ip:a", 3)
        assert auth_api._reset_rate_ok("ip:b", 3) is True


# ── /password-reset/request — anti-enumeration ───────────────────────────────

class TestRequestIsNonEnumerable:
    @pytest.mark.asyncio
    async def test_unknown_account(self, no_mail):
        db = _FakeDB([[]])
        out = await auth_api.password_reset_request(
            auth_api.PasswordResetRequest(identifier="nobody"), _FakeRequest(), db)
        assert out == auth_api._RESET_GENERIC
        assert no_mail == []

    @pytest.mark.asyncio
    async def test_known_account_without_email(self, no_mail):
        db = _FakeDB([[_FakeUser(email=None)]])
        out = await auth_api.password_reset_request(
            auth_api.PasswordResetRequest(identifier="dblagbro"), _FakeRequest(), db)
        assert out == auth_api._RESET_GENERIC
        assert no_mail == []

    @pytest.mark.asyncio
    async def test_real_account_gets_mail_but_same_response(self, no_mail):
        db = _FakeDB([[_FakeUser()]])
        out = await auth_api.password_reset_request(
            auth_api.PasswordResetRequest(identifier="dblagbro"), _FakeRequest(), db)
        assert out == auth_api._RESET_GENERIC          # identical to the misses
        assert len(no_mail) == 1
        assert no_mail[0]["to"] == "dblagbro@voipguru.org"

    @pytest.mark.asyncio
    async def test_mail_failure_is_not_surfaced(self, monkeypatch):
        async def boom(*_a, **_k):
            return False
        monkeypatch.setattr("app.utils.mailer.send_email_async", boom)
        db = _FakeDB([[_FakeUser()]])
        out = await auth_api.password_reset_request(
            auth_api.PasswordResetRequest(identifier="dblagbro"), _FakeRequest(), db)
        assert out == auth_api._RESET_GENERIC


class TestRequestStoresOnlyAHash:
    @pytest.mark.asyncio
    async def test_stored_row_has_no_raw_token(self, no_mail):
        db = _FakeDB([[_FakeUser()]])
        await auth_api.password_reset_request(
            auth_api.PasswordResetRequest(identifier="dblagbro"), _FakeRequest(), db)
        row = db.added[0]
        assert len(row.token_hash) == 64
        # the raw token only ever exists inside the email link
        link = no_mail[0]["html"]
        assert row.token_hash not in link
        assert row.expires_at > row.created_at

    @pytest.mark.asyncio
    async def test_link_token_hashes_to_the_stored_row(self, no_mail):
        db = _FakeDB([[_FakeUser()]])
        await auth_api.password_reset_request(
            auth_api.PasswordResetRequest(identifier="dblagbro"), _FakeRequest(), db)
        html = no_mail[0]["html"]
        raw = html.split("token=")[1].split('"')[0].split("<")[0].strip()
        assert auth_api._hash_reset_token(raw) == db.added[0].token_hash


class TestRequestRateLimit:
    @pytest.mark.asyncio
    async def test_per_ip_cap_stops_issuing(self, no_mail):
        req = _FakeRequest(ip="198.51.100.7")
        for _ in range(auth_api._RESET_MAX_PER_IP_PER_HOUR):
            await auth_api.password_reset_request(
                auth_api.PasswordResetRequest(identifier="x"), req, _FakeDB([[]]))
        db = _FakeDB([[_FakeUser()]])
        out = await auth_api.password_reset_request(
            auth_api.PasswordResetRequest(identifier="dblagbro"), req, db)
        assert out == auth_api._RESET_GENERIC
        assert no_mail == [], "throttled request must not send mail"


# ── /password-reset/confirm ──────────────────────────────────────────────────

class TestConfirmRejects:
    @pytest.mark.asyncio
    async def test_unknown_token(self):
        with pytest.raises(Exception) as e:
            await auth_api.password_reset_confirm(
                auth_api.PasswordResetConfirm(token="nope", new_password="abcd1234"),
                _FakeRequest(), _FakeDB([[]]))
        assert getattr(e.value, "status_code", None) == 400

    @pytest.mark.asyncio
    async def test_already_used_token(self):
        tok = _FakeToken(token_hash=auth_api._hash_reset_token("t"), used_at=time.time())
        with pytest.raises(Exception) as e:
            await auth_api.password_reset_confirm(
                auth_api.PasswordResetConfirm(token="t", new_password="abcd1234"),
                _FakeRequest(), _FakeDB([[tok]]))
        assert getattr(e.value, "status_code", None) == 400

    @pytest.mark.asyncio
    async def test_expired_token(self):
        tok = _FakeToken(token_hash=auth_api._hash_reset_token("t"), expires_in=-10)
        with pytest.raises(Exception) as e:
            await auth_api.password_reset_confirm(
                auth_api.PasswordResetConfirm(token="t", new_password="abcd1234"),
                _FakeRequest(), _FakeDB([[tok]]))
        assert getattr(e.value, "status_code", None) == 400

    @pytest.mark.asyncio
    async def test_short_password_rejected(self):
        with pytest.raises(Exception) as e:
            await auth_api.password_reset_confirm(
                auth_api.PasswordResetConfirm(token="t", new_password="short"),
                _FakeRequest(), _FakeDB([]))
        assert getattr(e.value, "status_code", None) == 400


class TestConfirmSucceeds:
    @pytest.mark.asyncio
    async def test_sets_hash_and_spends_token(self):
        tok = _FakeToken(token_hash=auth_api._hash_reset_token("good"))
        user = _FakeUser()
        db = _FakeDB([[tok], [user], []])
        out = await auth_api.password_reset_confirm(
            auth_api.PasswordResetConfirm(token="good", new_password="a-new-password"),
            _FakeRequest(), db)
        assert out["ok"] is True
        assert tok.used_at is not None, "token must be single-use"
        assert user.password_hash != "old-hash"
        assert "a-new-password" not in user.password_hash, "must store a hash"
        assert db.commits == 1

    @pytest.mark.asyncio
    async def test_other_outstanding_tokens_are_voided(self):
        tok = _FakeToken(token_hash=auth_api._hash_reset_token("good"))
        other = _FakeToken()
        db = _FakeDB([[tok], [_FakeUser()], [other]])
        await auth_api.password_reset_confirm(
            auth_api.PasswordResetConfirm(token="good", new_password="a-new-password"),
            _FakeRequest(), db)
        assert other.used_at is not None


# ── wiring / schema ──────────────────────────────────────────────────────────

class TestWiring:
    def test_user_has_email_column(self):
        from app.models.db_user import User
        assert "email" in User.__table__.columns

    def test_reset_token_table_shape(self):
        from app.models.db_user import PasswordResetToken as T
        cols = set(T.__table__.columns.keys())
        assert {"user_id", "token_hash", "expires_at", "used_at"} <= cols

    def test_email_column_migration_present(self):
        from pathlib import Path
        src = Path("app/models/database.py").read_text(encoding="utf-8")
        assert "ALTER TABLE users ADD COLUMN email TEXT" in src

    def test_reset_paths_bypass_allowed_paths_gate(self):
        """Both endpoints are unauthenticated and must not be gated."""
        from app.middleware.allowed_paths import _is_bypass_path
        assert _is_bypass_path("/api/auth/password-reset/request")
        assert _is_bypass_path("/api/auth/password-reset/confirm")

    def test_admin_reset_endpoint_exists(self):
        from app.api import users as users_api
        assert any(
            getattr(r, "path", "") == "/api/users/{user_id}/reset-password"
            for r in users_api.router.routes
        )

    def test_password_change_voids_reset_tokens(self):
        from pathlib import Path
        src = Path("app/api/users.py").read_text(encoding="utf-8")
        assert src.count("_invalidate_reset_tokens(db, user") >= 2, (
            "both PATCH and admin-reset must void outstanding links"
        )


class TestMailerIsSafe:
    def test_disabled_returns_false_and_does_not_raise(self, monkeypatch):
        from app.utils import mailer
        monkeypatch.setattr(mailer, "_cfg", lambda: {
            "enabled": False, "host": "h", "port": 587, "user": "", "password": "",
            "from_addr": "f@x", "helo": "",
        })
        assert mailer.send_email("to@x", "s", "<p>b</p>") is False

    def test_misconfigured_returns_false(self, monkeypatch):
        from app.utils import mailer
        monkeypatch.setattr(mailer, "_cfg", lambda: {
            "enabled": True, "host": "", "port": 587, "user": "", "password": "",
            "from_addr": "", "helo": "",
        })
        assert mailer.send_email("to@x", "s", "<p>b</p>") is False

    def test_smtp_explosion_is_swallowed(self, monkeypatch):
        from app.utils import mailer
        monkeypatch.setattr(mailer, "_cfg", lambda: {
            "enabled": True, "host": "h", "port": 587, "user": "u", "password": "p",
            "from_addr": "f@x", "helo": "x",
        })

        def boom(*_a, **_k):
            raise OSError("connection refused")

        monkeypatch.setattr(mailer.smtplib, "SMTP", boom)
        assert mailer.send_email("to@x", "s", "<p>b</p>") is False
