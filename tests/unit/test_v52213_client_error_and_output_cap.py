"""v5.22.13 pins — the three defects behind the 2026-08-18 Cohere lockout.

Story: a caller asked Cohere for ``max_tokens=32000`` against an 8192 cap.
1. The router had no OUTPUT-side capability gate, so Cohere was selected.
2. Cohere rejected it as a client error.
3. The handler wrapped that as HTTP 502, and ``classify_error`` matched the
   "502" in OUR OWN wrapper before reaching the bad_request patterns, so a
   CALLER's mistake was recorded as a PROVIDER failure. Repeated, exponential
   backoff locked a perfectly healthy provider out for ~17 hours.

All hermetic — no network, no DB, no live deployment.
"""
import pytest

from app.routing.circuit_breaker import classify_error
from app.routing.lmrh.types import CapabilityProfile
from app.routing.router import _capability_fit

# The verbatim error observed in production on 2026-08-18.
REAL_COHERE_502 = (
    "502: Upstream error before streaming began: "
    "litellm.ContextWindowExceededError: litellm.BadRequestError: "
    'CohereException - {"error_type":"TOO_MANY_TOKENS","message":"too many '
    "tokens: max tokens must be less than or equal to 8192, the maximum "
    'output length for this model - received 32000."}'
)


def test_wrapped_client_error_is_not_an_upstream_failure():
    """The regression that cost ~17h of Cohere availability."""
    assert classify_error(REAL_COHERE_502) == "bad_request"


@pytest.mark.parametrize("msg", [
    "litellm.BadRequestError: invalid_request_error",
    "500: ContextWindowExceededError from provider",
    "503: litellm.BadRequestError: ContentPolicyViolationError",
])
def test_client_error_markers_beat_a_wrapping_status(msg):
    assert classify_error(msg) == "bad_request"


@pytest.mark.parametrize("msg,expected", [
    ("502 Bad Gateway from upstream", "upstream_5xx"),
    ("503 Service Unavailable", "upstream_5xx"),
    ("529 overloaded_error", "upstream_5xx"),
    ("500 internal server error", "upstream_5xx"),
    ("401 invalid api key", "auth"),
    ("ReadTimeout: timed out", "timeout"),
    ("429 rate limit exceeded", "rate_limit"),
    ("connection refused", "network"),
])
def test_genuine_classes_are_unchanged(msg, expected):
    """The override must not swallow real upstream/auth/timeout errors."""
    assert classify_error(msg) == expected


def _profile(cap):
    return CapabilityProfile(
        provider_id="p", provider_type="cohere",
        model_id="command-r", max_output_tokens=cap,
    )


_FIT = dict(has_tools=False, needs_reasoning=False,
            has_images=False, est_input_tokens=None)


def test_provider_skipped_when_output_cap_too_small():
    reason = _capability_fit(_profile(8192), requested_max_tokens=32000, **_FIT)
    assert reason is not None
    assert "8192" in reason and "32000" in reason


def test_provider_kept_when_request_fits():
    assert _capability_fit(_profile(8192), requested_max_tokens=4000, **_FIT) is None


def test_unknown_cap_does_not_filter():
    """Catalog covers ~90% of models; unknown must never exclude a provider."""
    assert _capability_fit(_profile(None), requested_max_tokens=32000, **_FIT) is None


def test_absent_request_size_does_not_filter():
    """Call sites that don't pass the value keep their prior behaviour."""
    assert _capability_fit(_profile(8192), requested_max_tokens=None, **_FIT) is None
