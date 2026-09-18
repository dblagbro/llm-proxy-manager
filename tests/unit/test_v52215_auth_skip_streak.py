"""v5.22.15 — a 100%-failing provider must get skipped even when it is quiet.

The escalation to a DB-persisted ``auto_skip_until`` required
``PERSISTENT_AUTH_THRESHOLD`` (3) auth failures inside
``PERSISTENT_AUTH_WINDOW_SEC`` (30 min). That is a RATE test, and it has a
blind spot shaped exactly like a dead provider: repeated failure
deprioritises a provider, which lowers its traffic, which keeps it under a
rate-based threshold forever.

Observed live on Devin-Codex-Gmail: 0 successes in 55 requests over six
hours, ~3 requests per 30 minutes straddling the window boundary, and
``auto_skip_until`` frozen at 2026-08-20 — 16 days expired. The breaker
cycled open -> hold-down -> closed indefinitely, and because ``/health``
samples an instant, whichever sample landed in the closed phase reported the
node as 7/7 healthy.

The fix adds a consecutive-failure streak, which has no rate dependency.
"""

import pytest

from app.routing import circuit_breaker as cb


@pytest.fixture(autouse=True)
def _clean():
    for pid in ("slow-dead", "bursty", "recovering"):
        cb.clear_auth_failure(pid)
    yield
    for pid in ("slow-dead", "bursty", "recovering"):
        cb.clear_auth_failure(pid)


@pytest.fixture
def captured(monkeypatch):
    """Capture escalations instead of writing to the DB."""
    calls = []

    async def _fake_persist(provider_id, error_text):
        calls.append(provider_id)

    monkeypatch.setattr(cb, "_persist_auto_skip", _fake_persist)
    return calls


class TestSlowFailuresStillEscalate:
    @pytest.mark.asyncio
    async def test_failures_spread_beyond_the_window_still_escalate(self, captured, monkeypatch):
        """The regression. Each failure lands in its own 30-min window, so
        the windowed counter never exceeds 1 — exactly Codex-Gmail's shape."""
        base = [1_000_000.0]
        monkeypatch.setattr(cb.time, "time", lambda: base[0])

        for i in range(cb.PERSISTENT_AUTH_THRESHOLD):
            base[0] = 1_000_000.0 + i * (cb.PERSISTENT_AUTH_WINDOW_SEC * 2)
            await cb.record_auth_failure("slow-dead", "401 invalid api key")

        assert "slow-dead" in captured, (
            "a provider failing 100% of the time was never escalated to "
            "auto_skip because its traffic was too slow"
        )

    @pytest.mark.asyncio
    async def test_windowed_burst_still_escalates(self, captured):
        """The original rate-based path must keep working."""
        for _ in range(cb.PERSISTENT_AUTH_THRESHOLD):
            await cb.record_auth_failure("bursty", "401")
        assert "bursty" in captured

    @pytest.mark.asyncio
    async def test_first_failure_escalates(self, captured):
        """v5.22.20 changed this contract deliberately.

        This test used to assert that a single blip must NOT escalate. That
        protection was illusory: ``record_auth_failure`` sets
        ``hold_down_until = now + 86400`` unconditionally, so failure #1
        already pulls the provider for 24 hours. The threshold only decided
        whether that decision was written down — and because the open breaker
        then removes the provider from routing, failures #2 and #3 could not
        occur for another day. Measured on Devin-Codex-Gmail: one failure
        marked, then nothing for 24h, with auto_skip_until stale for a month.

        So escalation is now immediate. It is not more aggressive than the
        breaker it accompanies; it just makes that decision survive a restart.
        """
        await cb.record_auth_failure("bursty", "401")
        assert captured == ["bursty"]


class TestSuccessBreaksTheStreak:
    @pytest.mark.asyncio
    async def test_success_still_clears_the_streak_counter(self, monkeypatch):
        """Escalation no longer depends on the streak, but the counter still
        feeds the auth_failure_marked log line, so it must still be a
        *consecutive* count rather than a lifetime tally."""
        base = [2_000_000.0]
        monkeypatch.setattr(cb.time, "time", lambda: base[0])

        async def _noop(provider_id, error_text):
            return None

        monkeypatch.setattr(cb, "_persist_auto_skip", _noop)

        for _ in range(3):
            base[0] += cb.PERSISTENT_AUTH_WINDOW_SEC * 2
            await cb.record_auth_failure("recovering", "401")
        assert cb._auth_failure_streak["recovering"] == 3

        cb.clear_auth_failure("recovering")  # what a success does
        assert "recovering" not in cb._auth_failure_streak

        base[0] += cb.PERSISTENT_AUTH_WINDOW_SEC * 2
        await cb.record_auth_failure("recovering", "401")
        assert cb._auth_failure_streak["recovering"] == 1, "streak survived a success"

    def test_clear_removes_all_three_pieces_of_state(self):
        cb._auth_failed["recovering"] = {"since": 0, "last_error": "x"}
        cb._auth_failure_history["recovering"] = [1.0]
        cb._auth_failure_streak["recovering"] = 5
        cb.clear_auth_failure("recovering")
        assert "recovering" not in cb._auth_failed
        assert "recovering" not in cb._auth_failure_history
        assert "recovering" not in cb._auth_failure_streak
