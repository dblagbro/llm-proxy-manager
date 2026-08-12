"""v5.22.10 — login accepts the email address as well as the username.

Operator hit this on 2026-08-11: typing `dblagbro@voipguru.org` into the
sign-in form returned a bare 401 with no hint that the field wanted a
username, which read as "my password is wrong" and looked like a lockout.

Two hazards this must not open, since `users.email` has NO unique
constraint and is user-editable:

  1. An exact USERNAME match must win, or someone could set their own email
     to another account's username and capture that login.
  2. If two live accounts share an address, the email match is ambiguous and
     must be REFUSED — silently picking one could authenticate the wrong
     person.

Every rejection returns the same generic 401 so the endpoint stays
non-enumerable.
"""
import sys
import types

import pytest

sys.modules.setdefault("litellm", types.ModuleType("litellm"))

from app.api import auth as auth_api  # noqa: E402


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Serves queued result sets: first the username query, then the email one."""

    def __init__(self, *result_sets):
        self._q = list(result_sets)
        self.queries = 0

    async def execute(self, *_a, **_k):
        self.queries += 1
        return _Result(self._q.pop(0) if self._q else [])


class _FakeUser:
    def __init__(self, uid, username, email=None, role="admin"):
        self.id = uid
        self.username = username
        self.email = email
        self.role = role
        self.password_hash = f"hash-of-{username}"
        self.deleted_at = None


class _FakeResponse:
    def __init__(self):
        self.cookies = {}

    def set_cookie(self, name, value, **_kw):
        self.cookies[name] = value

    def delete_cookie(self, *_a, **_kw):
        pass


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    """Password is correct iff it matches the account's hash marker."""
    monkeypatch.setattr(auth_api, "verify_password",
                        lambda plain, hashed: hashed == f"hash-of-{plain}")

    async def fake_session(uid, username, role):
        return f"tok-{username}"

    monkeypatch.setattr(auth_api, "create_session", fake_session)


async def _login(identifier, password, db):
    return await auth_api.login(
        auth_api.LoginRequest(username=identifier, password=password),
        _FakeResponse(), db,
    )


def _status(exc) -> int:
    return getattr(exc.value, "status_code", None)


class TestUsernameStillWorks:
    @pytest.mark.asyncio
    async def test_plain_username_login(self):
        u = _FakeUser("u1", "dblagbro", "dblagbro@voipguru.org")
        out = await _login("dblagbro", "dblagbro", _FakeDB([u]))
        assert out["username"] == "dblagbro" and out["role"] == "admin"

    @pytest.mark.asyncio
    async def test_wrong_password_still_401(self):
        u = _FakeUser("u1", "dblagbro")
        with pytest.raises(Exception) as e:
            await _login("dblagbro", "nope", _FakeDB([u]))
        assert _status(e) == 401

    @pytest.mark.asyncio
    async def test_username_lookup_does_not_run_the_email_query(self):
        """No wasted round-trip when the username hits."""
        db = _FakeDB([_FakeUser("u1", "dblagbro")])
        await _login("dblagbro", "dblagbro", db)
        assert db.queries == 1


class TestEmailLogin:
    @pytest.mark.asyncio
    async def test_email_identifier_authenticates(self):
        u = _FakeUser("u1", "dblagbro", "dblagbro@voipguru.org")
        db = _FakeDB([], [u])                       # username miss, email hit
        out = await _login("dblagbro@voipguru.org", "dblagbro", db)
        assert out["username"] == "dblagbro"

    @pytest.mark.asyncio
    async def test_email_match_is_case_insensitive(self):
        u = _FakeUser("u1", "dblagbro", "dblagbro@voipguru.org")
        db = _FakeDB([], [u])
        out = await _login("DBlagbro@VoipGuru.ORG", "dblagbro", db)
        assert out["username"] == "dblagbro"

    @pytest.mark.asyncio
    async def test_surrounding_whitespace_tolerated(self):
        u = _FakeUser("u1", "dblagbro", "dblagbro@voipguru.org")
        db = _FakeDB([], [u])
        out = await _login("  dblagbro@voipguru.org  ", "dblagbro", db)
        assert out["username"] == "dblagbro"

    @pytest.mark.asyncio
    async def test_unknown_email_is_generic_401(self):
        with pytest.raises(Exception) as e:
            await _login("nobody@nowhere.test", "whatever", _FakeDB([], []))
        assert _status(e) == 401

    @pytest.mark.asyncio
    async def test_identifier_without_at_skips_the_email_query(self):
        """A plain miss must not trigger a second lookup."""
        db = _FakeDB([], [])
        with pytest.raises(Exception):
            await _login("no-such-user", "x", db)
        assert db.queries == 1


class TestHijackAndAmbiguityGuards:
    @pytest.mark.asyncio
    async def test_username_wins_over_someone_elses_email(self):
        """Attacker sets email='victim'; victim's USERNAME must still win.

        (Contrived identifier, but it is the shape of the attack: the email
        column is user-editable and not unique.)
        """
        victim = _FakeUser("v", "shared@x.com", email=None)
        attacker = _FakeUser("a", "attacker", email="shared@x.com")
        # username query returns the victim; email query would return attacker
        out = await _login("shared@x.com", "shared@x.com", _FakeDB([victim], [attacker]))
        assert out["username"] == "shared@x.com", "must authenticate the username owner"

    @pytest.mark.asyncio
    async def test_duplicate_email_is_refused_not_guessed(self):
        a = _FakeUser("a", "alice", "dup@x.com")
        b = _FakeUser("b", "bob", "dup@x.com")
        with pytest.raises(Exception) as e:
            await _login("dup@x.com", "alice", _FakeDB([], [a, b]))
        assert _status(e) == 401, "ambiguous email must not authenticate anyone"

    @pytest.mark.asyncio
    async def test_duplicate_email_refused_even_with_valid_password(self):
        """Right password for one of them still must not pick a winner."""
        a = _FakeUser("a", "alice", "dup@x.com")
        b = _FakeUser("b", "bob", "dup@x.com")
        with pytest.raises(Exception):
            await _login("dup@x.com", "bob", _FakeDB([], [a, b]))


class TestErrorParity:
    @pytest.mark.asyncio
    async def test_all_failures_share_one_message(self):
        """Unknown username, unknown email, bad password, ambiguous email —
        the client must not be able to tell them apart."""
        msgs = set()
        cases = [
            ("ghost", "x", _FakeDB([], [])),
            ("ghost@x.com", "x", _FakeDB([], [])),
            ("dblagbro", "wrong", _FakeDB([_FakeUser("u1", "dblagbro")])),
            ("dup@x.com", "alice", _FakeDB(
                [], [_FakeUser("a", "alice", "dup@x.com"),
                     _FakeUser("b", "bob", "dup@x.com")])),
        ]
        for ident, pw, db in cases:
            with pytest.raises(Exception) as e:
                await _login(ident, pw, db)
            assert _status(e) == 401
            msgs.add(str(getattr(e.value, "detail", "")))
        assert len(msgs) == 1, f"error messages differ and leak information: {msgs}"
