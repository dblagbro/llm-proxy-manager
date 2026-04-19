"""Unit tests for circuit breaker + hold-down."""
import asyncio
import pytest
from app.routing import circuit_breaker as cb


@pytest.fixture(autouse=True)
def reset_state():
    cb._local_states.clear()
    yield
    cb._local_states.clear()


@pytest.mark.asyncio
async def test_initial_state_closed():
    assert await cb.get_state("p1") == cb.CBState.CLOSED
    assert await cb.is_available("p1") is True


@pytest.mark.asyncio
async def test_opens_after_threshold():
    for _ in range(3):
        await cb.record_failure("p1")
    assert await cb.get_state("p1") == cb.CBState.OPEN
    assert await cb.is_available("p1") is False


@pytest.mark.asyncio
async def test_billing_error_opens_immediately():
    await cb.record_failure("p1", billing_error=True)
    assert await cb.get_state("p1") == cb.CBState.OPEN


@pytest.mark.asyncio
async def test_force_close_resets():
    await cb.record_failure("p1")
    await cb.record_failure("p1")
    await cb.record_failure("p1")
    await cb.force_close("p1")
    assert await cb.get_state("p1") == cb.CBState.CLOSED
    assert await cb.is_available("p1") is True


@pytest.mark.asyncio
async def test_is_billing_error_detection():
    assert cb.is_billing_error("quota exceeded") is True
    assert cb.is_billing_error("insufficient credit") is True
    assert cb.is_billing_error("normal response") is False
    assert cb.is_billing_error("429 rate limit") is True
