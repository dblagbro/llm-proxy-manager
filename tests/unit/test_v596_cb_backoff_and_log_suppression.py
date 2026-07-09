"""v5.9.6 — CB log suppression on re-open + exponential backoff.

Pre-fix, ``record_failure`` re-logged "circuit_breaker.opened" and reset
hold_down to the base value on EVERY failure while the CB was already
OPEN. With v5.3.9's auto-probe firing on every 60s timeout cycle, a
chronically-dead provider produced one noise line per cycle (~17 in
30 min) AND was retested every 120s — never escalating its hold-down
no matter how many consecutive cycles failed.

Post-fix:
- "circuit_breaker.opened" only emits on actual state transitions
  (CLOSED→OPEN or HALF_OPEN→OPEN), not while already OPEN
- ``consecutive_opens`` counter tracks transitions and resets on close
- ``hold_down`` is ``base * 2^min(consecutive_opens-1, 5)`` — so a
  provider that re-opens N times in a row escalates to 32× base (~64m
  at the default 120s), bridging the gap to billing-error (6h hold)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def _isolate_cb() -> Iterator[None]:
    from app.routing import circuit_breaker as cb
    cb._local_states.clear()
    cb._provider_overrides.clear()
    yield
    cb._local_states.clear()
    cb._provider_overrides.clear()


def test_record_failure_only_logs_on_transition(caplog) -> None:
    from app.routing import circuit_breaker as cb

    # Open the breaker — single threshold-crossing failure should log once.
    cb.set_provider_config("p1", hold_down_sec=120, failure_threshold=1)

    with caplog.at_level(logging.WARNING, logger="app.routing.circuit_breaker"):
        asyncio.run(cb.record_failure("p1"))
        first_opens = [r for r in caplog.records if "circuit_breaker.opened" in r.getMessage()]
        assert len(first_opens) == 1, first_opens

    # 10 more failures while CB is already OPEN — must not emit additional
    # "opened" log lines. This is the regression that v5.9.6 fixes.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.routing.circuit_breaker"):
        for _ in range(10):
            asyncio.run(cb.record_failure("p1"))
        extra_opens = [r for r in caplog.records if "circuit_breaker.opened" in r.getMessage()]
        assert extra_opens == [], f"expected no re-open logs while OPEN, got {extra_opens}"


def test_hold_down_does_not_reset_while_open() -> None:
    """Pre-fix bug: every failure while OPEN reset hold_down_until forward,
    so a busy dead provider could push its half-open timeout indefinitely
    past the next legitimate retest window."""
    from app.routing import circuit_breaker as cb

    cb.set_provider_config("p1", hold_down_sec=120, failure_threshold=1)

    asyncio.run(cb.record_failure("p1"))
    s = cb._get_local("p1")
    initial_hold_down = s.hold_down_until

    # While still OPEN (consecutive_opens=1), additional failures must not
    # advance hold_down_until — the test is "did the timestamp move."
    asyncio.run(cb.record_failure("p1"))
    asyncio.run(cb.record_failure("p1"))
    assert s.hold_down_until == initial_hold_down


def test_consecutive_opens_increments_only_on_transition() -> None:
    from app.routing import circuit_breaker as cb

    cb.set_provider_config("p1", hold_down_sec=120, failure_threshold=1)

    asyncio.run(cb.record_failure("p1"))
    s = cb._get_local("p1")
    assert s.consecutive_opens == 1
    assert s.state == cb.CBState.OPEN

    # 5 more failures while already OPEN — counter must stay at 1.
    for _ in range(5):
        asyncio.run(cb.record_failure("p1"))
    assert s.consecutive_opens == 1


def test_exponential_backoff_on_cycled_reopens() -> None:
    """Simulate the chronically-dead provider pattern: OPEN → (timeout) →
    HALF_OPEN → probe fails → re-OPEN. After N such cycles, hold_down
    should be ``base * 2^min(N-1, 5)``."""
    from app.routing import circuit_breaker as cb

    cb.set_provider_config("p1", hold_down_sec=100, failure_threshold=1)
    s = cb._get_local("p1")

    # Cycle 1: 100s
    asyncio.run(cb.record_failure("p1"))
    assert s.consecutive_opens == 1
    held_1 = s.hold_down_until - s.opened_at
    assert 99 <= held_1 <= 101  # accommodate floating-point drift

    # Cycle 2: 200s
    s.state = cb.CBState.HALF_OPEN  # simulate the timeout-driven transition
    asyncio.run(cb.record_failure("p1"))
    assert s.consecutive_opens == 2
    held_2 = s.hold_down_until - s.opened_at
    assert 199 <= held_2 <= 201

    # Cycle 4: 800s (2^3 × base)
    s.state = cb.CBState.HALF_OPEN
    asyncio.run(cb.record_failure("p1"))
    s.state = cb.CBState.HALF_OPEN
    asyncio.run(cb.record_failure("p1"))
    assert s.consecutive_opens == 4
    held_4 = s.hold_down_until - s.opened_at
    assert 799 <= held_4 <= 801

    # Cycle 8: capped at 2^5 × base = 3200s (not 12800)
    for _ in range(4):
        s.state = cb.CBState.HALF_OPEN
        asyncio.run(cb.record_failure("p1"))
    assert s.consecutive_opens == 8
    held_8 = s.hold_down_until - s.opened_at
    assert 3199 <= held_8 <= 3201


def test_consecutive_opens_resets_on_recovery() -> None:
    """A provider that comes back must drop its backoff state. Otherwise
    a previously-flaky provider would still be on a 32× hold-down for its
    *next* failure after recovery."""
    from app.routing import circuit_breaker as cb

    cb.set_provider_config("p1", hold_down_sec=120, failure_threshold=1)
    s = cb._get_local("p1")

    # Two cycles of re-open
    asyncio.run(cb.record_failure("p1"))
    s.state = cb.CBState.HALF_OPEN
    asyncio.run(cb.record_failure("p1"))
    assert s.consecutive_opens == 2

    # Provider recovers: enough successes to close the breaker.
    s.state = cb.CBState.HALF_OPEN
    s.successes = 0
    needed = cb.settings.circuit_breaker_success_needed
    for _ in range(needed):
        asyncio.run(cb.record_success("p1"))
    assert s.state == cb.CBState.CLOSED
    assert s.consecutive_opens == 0


def test_force_close_resets_consecutive_opens() -> None:
    from app.routing import circuit_breaker as cb

    cb.set_provider_config("p1", hold_down_sec=120, failure_threshold=1)
    asyncio.run(cb.record_failure("p1"))
    s = cb._get_local("p1")
    assert s.consecutive_opens == 1

    asyncio.run(cb.force_close("p1"))
    assert s.consecutive_opens == 0
    assert s.state == cb.CBState.CLOSED
