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
    cb._auth_failed.clear()
    yield
    cb._local_states.clear()
    cb._auth_failed.clear()


class TestAuthErrorClassifier:
    """v2.7.8 BUG-002: is_auth_error classifies errors as permanent
    (admin must re-key) vs transient."""

    def test_invalid_api_key(self):
        assert cb.is_auth_error("litellm.AuthenticationError: invalid x-api-key") is True

    def test_oauth_invalid_credentials(self):
        assert cb.is_auth_error('{"type":"authentication_error","message":"Invalid authentication credentials"}') is True

    def test_403_permission_denied(self):
        assert cb.is_auth_error("HTTP 403: permission_denied") is True

    def test_invalid_grant(self):
        assert cb.is_auth_error('{"error":"invalid_grant"}') is True

    def test_missing_gemini_key(self):
        assert cb.is_auth_error("litellm.APIConnectionError: Missing Gemini API key. Set the GEMINI_API_KEY...") is True

    def test_missing_openai_key(self):
        assert cb.is_auth_error("OpenAIException - The api_key client option must be set...") is True

    def test_rate_limit_not_auth(self):
        # 429 / rate limit is transient, NOT an auth error
        assert cb.is_auth_error("litellm.RateLimitError: 429 Too Many Requests") is False

    def test_network_error_not_auth(self):
        assert cb.is_auth_error("litellm.APIConnectionError: connection refused") is False

    def test_empty_returns_false(self):
        assert cb.is_auth_error("") is False
        assert cb.is_auth_error(None) is False  # type: ignore


class TestAuthFailureLifecycle:
    """Auth failures open the breaker for 24h and persist in a separate map."""

    def test_record_auth_failure_marks_provider(self):
        _run(cb.record_auth_failure("p1", "401 invalid_token"))
        info = cb.get_auth_failure("p1")
        assert info is not None
        assert "401 invalid_token" in info["last_error"]
        assert info["since"] > 0

    def test_record_auth_failure_opens_breaker(self):
        _run(cb.record_auth_failure("p2", "401"))
        assert cb._get_local("p2").state == cb.CBState.OPEN

    def test_clear_auth_failure(self):
        _run(cb.record_auth_failure("p3", "401"))
        assert cb.get_auth_failure("p3") is not None
        cb.clear_auth_failure("p3")
        assert cb.get_auth_failure("p3") is None

    def test_get_all_auth_failures(self):
        _run(cb.record_auth_failure("pA", "401"))
        _run(cb.record_auth_failure("pB", "403"))
        all_fails = cb.get_all_auth_failures()
        assert "pA" in all_fails and "pB" in all_fails
        assert len(all_fails) == 2


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
