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


def test_billing_error_uses_six_hour_hold_down():
    # v3.0.53: billing errors need operator intervention; 6h hold avoids
    # 24×/day probe-and-fail noise on quota-exhausted providers.
    async def _():
        await cb.record_failure("p1", billing_error=True)
        states = cb.get_all_states()
        # Within 5h: still in hold-down. After 6h+ would be 0.
        assert states["p1"]["hold_down_remaining"] > 5 * 3600
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
    # True billing / quota-exhausted signals → open breaker for 6h (v3.0.53)
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


# v3.0.75 — coarse error-class taxonomy for activity-log filtering
class TestClassifyError:
    def test_empty_string_is_unknown(self):
        assert cb.classify_error("") == "unknown"
        assert cb.classify_error(None) == "unknown"  # type: ignore[arg-type]

    def test_auth_takes_precedence(self):
        # Auth and 401 share status text; auth wins so the operator sees
        # the actionable category (re-auth needed) not generic upstream_5xx.
        assert cb.classify_error("HTTP 401: invalid_authentication") == "auth"
        assert cb.classify_error("authentication_error: bad token") == "auth"

    def test_billing_classification(self):
        assert cb.classify_error("insufficient_quota") == "billing"
        assert cb.classify_error("Payment Required") == "billing"
        assert cb.classify_error("You have exhausted your subscription") == "billing"

    def test_rate_limit_classification(self):
        assert cb.classify_error("429 Too Many Requests") == "rate_limit"
        assert cb.classify_error("rate limit exceeded") == "rate_limit"
        assert cb.classify_error("ratelimit_error") == "rate_limit"
        assert cb.classify_error("throttled") == "rate_limit"

    def test_timeout_classification(self):
        assert cb.classify_error("connection timed out") == "timeout"
        assert cb.classify_error("read timeout") == "timeout"
        assert cb.classify_error("deadline exceeded") == "timeout"

    def test_network_classification(self):
        assert cb.classify_error("Connection refused") == "network"
        assert cb.classify_error("connection reset by peer") == "network"
        assert cb.classify_error("Name or service not known") == "network"
        assert cb.classify_error("Temporary failure in name resolution") == "network"

    def test_httpx_exception_names_classified_as_network(self):
        """v3.0.88: httpx exception names (ReadError, WriteError, etc.)
        used to fall through to ``unknown`` during the 2026-05-06 22s
        upstream Anthropic blip. httpx docs ReadError as 'Failed to
        receive data from the network' — that's network class."""
        assert cb.classify_error("ReadError (no message)") == "network"
        assert cb.classify_error("httpx.ReadError") == "network"
        assert cb.classify_error("WriteError") == "network"
        assert cb.classify_error("RemoteProtocolError: server disconnected") == "network"
        assert cb.classify_error("LocalProtocolError: bad chunked encoding") == "network"
        assert cb.classify_error("ProxyError: failed to connect") == "network"

    def test_upstream_5xx_classification(self):
        assert cb.classify_error("502 Bad Gateway") == "upstream_5xx"
        assert cb.classify_error("503 Service Unavailable") == "upstream_5xx"
        assert cb.classify_error("Internal Server Error") == "upstream_5xx"

    def test_bad_request_classification(self):
        assert cb.classify_error("400: invalid_request") == "bad_request"
        assert cb.classify_error("validation error: field x missing") == "bad_request"

    def test_litellm_exception_names_classified_as_bad_request(self):
        """v3.0.89: SDK exception names are camelcase one-word, not the
        space-separated form earlier patterns expected. The 7d scan
        found ``ContextWindowExceededError`` falling through to unknown."""
        assert cb.classify_error(
            "litellm.ContextWindowExceededError: Input tokens 200000 > 100000"
        ) == "bad_request"
        assert cb.classify_error(
            "litellm.BadRequestError: invalid model"
        ) == "bad_request"
        assert cb.classify_error(
            "ContentPolicyViolationError: blocked"
        ) == "bad_request"
        assert cb.classify_error("UnsupportedParamsError: bar") == "bad_request"

    def test_unknown_fallthrough(self):
        # Truly novel error string falls through to unknown rather than
        # accidentally bucketing into a more specific category.
        assert cb.classify_error("some bizarre new error from upstream") == "unknown"

    def test_billing_429_routes_to_billing_not_rate_limit(self):
        """When a single error string is both billing-shaped (insufficient_quota)
        AND rate-limit-shaped (429), billing should win because it's the
        actionable signal — operator needs to add credit, not back off."""
        assert cb.classify_error("429 insufficient_quota") == "billing"
