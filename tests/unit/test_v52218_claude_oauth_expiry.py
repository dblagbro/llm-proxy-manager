"""v5.22.18 — claude-oauth tokens must refresh without needing a request.

The deadlock this closes
------------------------
claude-oauth access tokens live ~8h, and the only thing that refreshed them
was the lazy-on-401 path inside request dispatch. ``record_auth_failure``
sets a 24-hour breaker hold-down and ``is_available()`` returns False for its
duration, so an auth-failed provider leaves the routing pool entirely:

    auth failure -> breaker open 24h -> no requests -> no 401 -> no refresh
        -> staler token -> hold-down lifts -> 401 -> breaker open 24h -> ...

Observed live 2026-09-10: both Anthropic providers sat in hold-down holding
valid 108-character refresh tokens, VG for two full cycles, with the cluster
reporting 5/7 and 4/7 providers. Cursor had already been moved off
lazy-on-401 for this exact reason; claude-oauth had not.

``test_successful_refresh_releases_the_breaker`` is the test that matters. A
refreshed token sitting behind a 24-hour open breaker is still a dead
provider, so refreshing without releasing the breaker would fix nothing.
"""

import asyncio
import time

import pytest

from app.monitoring import claude_oauth_expiry_monitor as mon


class _FakeProvider:
    def __init__(self, pid, name, expires_in_sec, refresh_token="r" * 108):
        self.id = pid
        self.name = name
        self.provider_type = "claude-oauth"
        self.enabled = True
        self.deleted_at = None
        self.oauth_refresh_token = refresh_token
        self.oauth_expires_at = (
            None if expires_in_sec is None else time.time() + expires_in_sec
        )


class _FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _FakeDB:
    def __init__(self, providers):
        self._providers = providers
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, *_a, **_k):
        return _FakeResult(self._providers)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


@pytest.fixture
def harness(monkeypatch):
    """Wire fakes into the function-local imports the sweep uses."""
    state = {"refreshed": [], "cleared": [], "closed": [], "raise_for": set()}

    def _install(providers):
        db = _FakeDB(providers)
        import app.models.database as dbmod
        import app.providers.claude_oauth_flow as flow
        import app.routing.circuit_breaker as cb

        monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: db, raising=False)

        async def fake_refresh(provider, _db):
            if provider.id in state["raise_for"]:
                raise RuntimeError("invalid_grant: refresh token revoked")
            state["refreshed"].append(provider.id)

        monkeypatch.setattr(flow, "refresh_and_persist", fake_refresh)
        monkeypatch.setattr(
            cb, "clear_auth_failure", lambda pid: state["cleared"].append(pid)
        )

        async def fake_force_close(pid):
            state["closed"].append(pid)

        monkeypatch.setattr(cb, "force_close", fake_force_close)
        return db

    state["install"] = _install
    return state


class TestBreaksTheDeadlock:
    @pytest.mark.asyncio
    async def test_successful_refresh_releases_the_breaker(self, harness):
        """THE regression test. A fresh token behind a 24h open breaker is
        still a dead provider."""
        harness["install"]([_FakeProvider("p1", "Anthropic-VG", expires_in_sec=60)])
        await mon._run_one_sweep()

        assert harness["refreshed"] == ["p1"]
        assert harness["cleared"] == ["p1"], "auth-failure state not cleared"
        assert harness["closed"] == ["p1"], "breaker not force-closed"

    @pytest.mark.asyncio
    async def test_refresh_runs_for_an_already_expired_token(self, harness):
        """The stuck case: expiry is in the past, well beyond the window."""
        harness["install"]([_FakeProvider("p1", "VG", expires_in_sec=-45 * 3600)])
        await mon._run_one_sweep()
        assert harness["refreshed"] == ["p1"]

    def test_does_not_depend_on_request_dispatch(self):
        """Structural: the sweep must not reach into the request path, or an
        open breaker would gate it again.

        Checked against the AST rather than the raw text — the module
        docstring names those symbols on purpose, to explain the deadlock.
        """
        import ast
        from pathlib import Path

        tree = ast.parse(
            Path("app/monitoring/claude_oauth_expiry_monitor.py").read_text()
        )
        imported: list[str] = []
        called: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
            elif isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name:
                    called.append(name)

        assert not any("_messages_streaming" in m for m in imported), (
            f"sweep imports request-dispatch code: {imported}"
        )
        assert "is_available" not in called, (
            "sweep consults the circuit breaker — an open breaker would gate "
            "the very refresh that unsticks it"
        )


class TestWindowLogic:
    @pytest.mark.asyncio
    async def test_token_outside_the_window_is_left_alone(self, harness):
        harness["install"](
            [_FakeProvider("p1", "fresh", expires_in_sec=mon._REFRESH_WINDOW_SEC + 3600)]
        )
        await mon._run_one_sweep()
        assert harness["refreshed"] == []

    @pytest.mark.asyncio
    async def test_token_inside_the_window_is_refreshed(self, harness):
        harness["install"](
            [_FakeProvider("p1", "due", expires_in_sec=mon._REFRESH_WINDOW_SEC - 60)]
        )
        await mon._run_one_sweep()
        assert harness["refreshed"] == ["p1"]

    @pytest.mark.asyncio
    async def test_null_expiry_is_treated_as_due(self, harness):
        """A NULL expiry cannot be reasoned about; leaving it alone is how a
        provider silently rots."""
        harness["install"]([_FakeProvider("p1", "unknown", expires_in_sec=None)])
        await mon._run_one_sweep()
        assert harness["refreshed"] == ["p1"]


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_provider_without_refresh_token_is_skipped(self, harness):
        harness["install"]([_FakeProvider("p1", "no-token", 60, refresh_token=None)])
        await mon._run_one_sweep()
        assert harness["refreshed"] == []
        assert mon.get_last_sweep()["checked"] == 0

    @pytest.mark.asyncio
    async def test_a_dead_refresh_token_does_not_abort_the_sweep(self, harness):
        """One unrecoverable provider must not stop the others being fixed."""
        h = harness
        h["raise_for"].add("dead")
        h["install"](
            [
                _FakeProvider("dead", "revoked", 60),
                _FakeProvider("live", "recoverable", 60),
            ]
        )
        await mon._run_one_sweep()

        assert h["refreshed"] == ["live"]
        assert h["closed"] == ["live"], "breaker released for a failed refresh"
        sweep = mon.get_last_sweep()
        assert sweep["refreshed"] == 1 and sweep["failed"] == 1

    @pytest.mark.asyncio
    async def test_failed_refresh_rolls_back(self, harness):
        h = harness
        h["raise_for"].add("dead")
        db = h["install"]([_FakeProvider("dead", "revoked", 60)])
        await mon._run_one_sweep()
        assert db.rollbacks == 1 and db.commits == 0


class TestWiring:
    def test_worker_is_started_at_boot(self):
        src = __import__("pathlib").Path("app/main.py").read_text()
        assert "claude_oauth_expiry_monitor import start" in src

    def test_start_is_idempotent(self):
        mon.stop()
        assert mon._TASK is None or mon._TASK.done()

    @pytest.mark.asyncio
    async def test_start_then_stop(self):
        mon.start()
        first = mon._TASK
        mon.start()
        assert mon._TASK is first, "start() spawned a second task"
        mon.stop()
        await asyncio.sleep(0)

    def test_cadence_gives_multiple_attempts_before_expiry(self):
        """A single pre-expiry attempt would make one network blip fatal."""
        assert mon._REFRESH_WINDOW_SEC // mon._SWEEP_INTERVAL_SEC >= 3
