"""v5.22.15 — failed admin logins must be rate-limited.

``POST /api/auth/login`` had no attempt limit. ``ip_block.py`` names it in the
always-allow list on purpose (so an operator can never permanently lock
themselves out), which meant the app's one IP control explicitly did not
cover the one endpoint that guards a password. bcrypt's cost was the only
brake.

The properties that matter are as much about not breaking the operator as
about stopping the attacker, so both directions are tested: a legitimate
typo-then-succeed must never be penalised, and a lockout must always expire.
"""

import time

import pytest

from app.auth import login_throttle
from app.auth.login_throttle import (
    LOCKOUT_SEC,
    MAX_FAILURES,
    WINDOW_SEC,
    record_failure,
    record_success,
    seconds_remaining,
)

IP = "203.0.113.7"
OTHER_IP = "203.0.113.8"


@pytest.fixture(autouse=True)
def _clean():
    login_throttle.reset()
    yield
    login_throttle.reset()


class TestLockout:
    def test_not_locked_before_the_threshold(self):
        for _ in range(MAX_FAILURES - 1):
            record_failure(IP)
        assert seconds_remaining(IP) == 0

    def test_locks_at_the_threshold(self):
        for _ in range(MAX_FAILURES):
            record_failure(IP)
        assert seconds_remaining(IP) > 0

    def test_lockout_always_expires(self):
        """A permanent lock on this endpoint is unrecoverable without shell
        access — ip_block.py's exemption exists for exactly that reason."""
        now = time.time()
        for _ in range(MAX_FAILURES):
            record_failure(IP, now=now)
        assert seconds_remaining(IP, now=now) > 0
        assert seconds_remaining(IP, now=now + LOCKOUT_SEC + 1) == 0

    def test_failures_outside_the_window_do_not_accumulate(self):
        """Slow, spread-out typos over days must never add up to a lockout."""
        now = time.time()
        for i in range(MAX_FAILURES * 3):
            record_failure(IP, now=now + i * (WINDOW_SEC + 1))
        assert seconds_remaining(IP, now=now + MAX_FAILURES * 3 * (WINDOW_SEC + 1)) == 0


class TestOperatorIsNotPunished:
    def test_success_clears_the_counter(self):
        for _ in range(MAX_FAILURES - 1):
            record_failure(IP)
        record_success(IP)
        for _ in range(MAX_FAILURES - 1):
            record_failure(IP)
        assert seconds_remaining(IP) == 0

    def test_success_releases_an_existing_lock(self):
        for _ in range(MAX_FAILURES):
            record_failure(IP)
        assert seconds_remaining(IP) > 0
        record_success(IP)
        assert seconds_remaining(IP) == 0


class TestPerIPIsolation:
    def test_one_attacker_cannot_lock_out_everyone(self):
        """Keying on username would let an attacker deny the operator their
        own account for free. Keying on IP is what avoids that."""
        for _ in range(MAX_FAILURES * 2):
            record_failure(OTHER_IP)
        assert seconds_remaining(OTHER_IP) > 0
        assert seconds_remaining(IP) == 0


class TestMemoryIsBounded:
    def test_tracking_map_cannot_grow_without_limit(self):
        """X-Forwarded-For is attacker-controlled, so the key space is too."""
        now = time.time()
        for i in range(login_throttle.MAX_TRACKED_IPS + 500):
            record_failure(f"10.0.{i // 256}.{i % 256}", now=now + i * 0.001)
        assert len(login_throttle._failures) <= login_throttle.MAX_TRACKED_IPS


class TestHandlerIsWiredUp:
    """The module is useless if the endpoint does not call it. This repo
    already uses static-source pins for exactly this class of regression."""

    def test_login_handler_calls_the_throttle(self):
        src = __import__("pathlib").Path("app/api/auth.py").read_text()
        assert "login_throttle.seconds_remaining" in src, "no pre-check in login"
        assert "login_throttle.record_failure" in src, "failures not counted"
        assert "login_throttle.record_success" in src, "success does not clear"
        assert "429" in src, "no Too Many Requests response"
