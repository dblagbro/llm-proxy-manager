"""Unit tests for circuit breaker + hold-down.

Async tests are run via a thread-pool executor so they always get a clean
event loop, regardless of what Playwright (session-scoped browser fixture)
has left in the main thread's asyncio state.
"""
import asyncio
import concurrent.futures

import pytest
from app.routing import circuit_breaker as cb


def _run(coro):
    """Run *coro* in a fresh thread that has no event-loop state."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@pytest.fixture(autouse=True)
def reset_state():
    cb._local_states.clear()
    yield
    cb._local_states.clear()


def test_initial_state_closed():
    async def _():
        assert await cb.get_state("p1") == cb.CBState.CLOSED
        assert await cb.is_available("p1") is True
    _run(_())


def test_opens_after_threshold():
    async def _():
        for _ in range(3):
            await cb.record_failure("p1")
        assert await cb.get_state("p1") == cb.CBState.OPEN
        assert await cb.is_available("p1") is False
    _run(_())


def test_billing_error_opens_immediately():
    async def _():
        await cb.record_failure("p1", billing_error=True)
        assert await cb.get_state("p1") == cb.CBState.OPEN
    _run(_())


def test_force_close_resets():
    async def _():
        await cb.record_failure("p1")
        await cb.record_failure("p1")
        await cb.record_failure("p1")
        await cb.force_close("p1")
        assert await cb.get_state("p1") == cb.CBState.CLOSED
        assert await cb.is_available("p1") is True
    _run(_())


def test_is_billing_error_detection():
    # True billing / quota-exhausted signals → open breaker for 1h
    assert cb.is_billing_error("quota exceeded") is True
    assert cb.is_billing_error("insufficient credit") is True
    assert cb.is_billing_error("insufficient_quota") is True
    assert cb.is_billing_error("billing issue") is True
    assert cb.is_billing_error("Payment Required") is True
    assert cb.is_billing_error("You have exhausted your subscription") is True

    # Transient throttling → must flow through retry loop, NOT fail-fast
    assert cb.is_billing_error("normal response") is False
    assert cb.is_billing_error("429 Too Many Requests") is False
    assert cb.is_billing_error("rate limit exceeded, retry later") is False

    # Billing-scoped 429s still match via the specific substring
    assert cb.is_billing_error("429 insufficient_quota") is True
