"""v5.22.49 — auth-failure breaker uses a short exponential hold, not a flat 24h.

Surfaced 2026-10-31 while fixing the recurring mass-trip (both nodes fell to
0/7 healthy). Two providers were stuck OPEN with `failures=0` and ~12h of
hold remaining: `Devin-Codex-Gmail` (the OpenAI **Daybreak** account, OAuth
valid 8 more days) and `Devin-Cohere` (API key valid). A single transient
auth error had benched each for a full day.

Root cause: `record_auth_failure` set `hold_down_until = now + 86400` on
failure #1. The breaker therefore never reached half-open within the hold, so
`record_success` could never close it — a usable provider was sidelined for
24h on one blip. The *durable* 24h sideline is supposed to come only from the
streak → `_persist_auto_skip` escalation (3 failures in 30 min), not from the
first in-memory failure.

Fix: the in-memory hold is now `base × 2^(streak-1)` (capped 32×), matching
`record_failure`'s backoff, so a blip recovers in ~base seconds while a
genuinely dead provider still backs off and escalates to the durable
auto_skip. `record_success` and `force_close` also clear the auth-failure
streak/map so recovery is clean.
"""
from __future__ import annotations

import asyncio

import app.routing.circuit_breaker as cb
from app.config import settings


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _reset(pid: str):
    cb._local_states.pop(pid, None)
    cb.clear_auth_failure(pid)


def test_first_auth_failure_uses_short_hold_not_24h():
    import time
    pid = "v52249-p1"
    _reset(pid)
    _run(cb.record_auth_failure(pid, "401 invalid_token"))
    s = cb._get_local(pid)
    base = cb._hold_down_sec(pid)
    remaining = s.hold_down_until - time.time()
    # Far below the old flat 24h; first failure == one base interval.
    assert remaining <= base + 5, f"hold {remaining}s should be ~base {base}s"
    assert remaining < 3600, "first auth failure must not set a multi-hour hold"
    assert s.state == cb.CBState.OPEN
    _reset(pid)


def test_auth_failure_hold_backs_off_exponentially():
    pid = "v52249-p2"
    _reset(pid)
    base = cb._hold_down_sec(pid)
    import time
    _run(cb.record_auth_failure(pid, "401"))
    h1 = cb._get_local(pid).hold_down_until - time.time()
    _run(cb.record_auth_failure(pid, "401"))
    h2 = cb._get_local(pid).hold_down_until - time.time()
    assert h1 <= base + 5
    assert h2 >= base * 2 - 5, f"second failure should back off (got {h2}s)"
    _reset(pid)


def test_record_success_clears_auth_failure_and_closes():
    pid = "v52249-p3"
    _reset(pid)
    _run(cb.record_auth_failure(pid, "401"))
    assert cb.get_auth_failure(pid) is not None
    # Drive it to half-open then feed the successes needed to close.
    s = cb._get_local(pid)
    s.state = cb.CBState.HALF_OPEN
    for _ in range(settings.circuit_breaker_success_needed):
        _run(cb.record_success(pid))
    assert cb._get_local(pid).state == cb.CBState.CLOSED
    assert cb.get_auth_failure(pid) is None, "success must clear the auth-failure map"
    _reset(pid)


def test_force_close_clears_auth_failure():
    pid = "v52249-p4"
    _reset(pid)
    _run(cb.record_auth_failure(pid, "401"))
    assert cb.get_auth_failure(pid) is not None
    _run(cb.force_close(pid))
    assert cb._get_local(pid).state == cb.CBState.CLOSED
    assert cb.get_auth_failure(pid) is None
    assert cb._get_local(pid).hold_down_until == 0.0
    _reset(pid)


def test_streak_still_escalates_to_auto_skip():
    """The short in-memory hold must NOT weaken the durable escalation:
    PERSISTENT_AUTH_THRESHOLD consecutive failures still persists auto_skip."""
    pid = "v52249-p5"
    _reset(pid)
    calls = {"n": 0}

    async def fake_persist(provider_id, error_text):
        calls["n"] += 1

    orig = cb._persist_auto_skip
    cb._persist_auto_skip = fake_persist
    try:
        for _ in range(cb.PERSISTENT_AUTH_THRESHOLD):
            _run(cb.record_auth_failure(pid, "401"))
    finally:
        cb._persist_auto_skip = orig
    assert calls["n"] >= 1, "streak crossing threshold must still escalate to auto_skip"
    _reset(pid)
