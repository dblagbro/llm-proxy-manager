"""v5.9.7 — ``get_state`` must enforce ``hold_down_until``, not just the
static ``circuit_breaker_timeout_sec``.

Latent since v5.3.9 (auto-probe introduction). The OPEN→HALF_OPEN
transition only checked ``opened_at + circuit_breaker_timeout_sec`` (60s
default), ignoring ``hold_down_until``. So:

- v5.9.6 exponential backoff: hold_down grew to 3840s on cycle 6+, but
  the half-open transition still fired every 60s — probe storm at the
  computed-but-unused cap. (caught live on Grok-Web-Devin: 20 CB-open
  cycles in 30 min with hold_down=3840s logged)
- v3.0.53 billing-error 6h hold: same bug, billing-error opens with
  hold_down=21600s but auto-probe still fires every 60s for hours.

Fix: ``ready_at = max(opened_at + circuit_breaker_timeout_sec, hold_down_until)``.
Transient blips still respect the snappy 60s probe; backed-off providers
wait their actual hold_down.
"""
from __future__ import annotations

import asyncio
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


def test_get_state_blocks_half_open_when_hold_down_pending() -> None:
    """A provider with a 1-hour hold_down must NOT transition to
    half-open just because the global 60s timeout has elapsed."""
    from app.routing import circuit_breaker as cb
    import time

    s = cb._get_local("p1")
    now = time.time()
    s.state = cb.CBState.OPEN
    s.opened_at = now - 120  # 120s ago — past the 60s timeout
    s.hold_down_until = now + 3600  # but 1h hold_down still has 1h left

    state = asyncio.run(cb.get_state("p1"))
    assert state == cb.CBState.OPEN, (
        "CB transitioned to half-open while hold_down still pending"
    )


def test_get_state_transitions_when_both_thresholds_passed() -> None:
    from app.routing import circuit_breaker as cb
    import time

    s = cb._get_local("p1")
    now = time.time()
    s.state = cb.CBState.OPEN
    s.opened_at = now - 120
    s.hold_down_until = now - 10  # both thresholds in the past

    state = asyncio.run(cb.get_state("p1"))
    assert state == cb.CBState.HALF_OPEN


def test_get_state_respects_short_hold_down_for_transient() -> None:
    """A transient (single-cycle) failure has hold_down_until ≈
    opened_at + base. The timeout (60s) and hold_down should still
    cooperate — whichever is later wins."""
    from app.routing import circuit_breaker as cb
    import time

    s = cb._get_local("p1")
    now = time.time()
    s.state = cb.CBState.OPEN
    s.opened_at = now - 70  # past 60s timeout
    s.hold_down_until = now - 5  # also past hold_down

    state = asyncio.run(cb.get_state("p1"))
    assert state == cb.CBState.HALF_OPEN


def test_billing_error_hold_down_now_enforced() -> None:
    """Regression: pre-fix the 6h billing-error hold_down was logged
    but the auto-probe still fired every 60s. This codifies that the
    documented behavior actually holds."""
    from app.routing import circuit_breaker as cb
    import time

    asyncio.run(cb.record_failure("p1", billing_error=True))
    s = cb._get_local("p1")
    assert s.state == cb.CBState.OPEN
    # Simulate 10 minutes elapsed — well past the 60s timeout but tiny
    # fraction of the 6h hold_down.
    s.opened_at = time.time() - 600

    state = asyncio.run(cb.get_state("p1"))
    assert state == cb.CBState.OPEN, "billing-error CB should remain OPEN"
