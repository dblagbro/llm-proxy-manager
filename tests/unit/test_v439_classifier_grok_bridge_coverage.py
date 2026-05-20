"""v4.3.9 — BUG-048: extend classify_error to bucket grok-web bridge
errors instead of letting them fall through to ``unknown``.

Closes BUG-048 surfaced during the 2026-05-20 proactive sweep. The
24h activity log had 47 errors with ``error_class=unknown`` — all
Grok-Web-Devin bridge failures with shapes the existing pattern
lists didn't recognise:

  1. `GrokWebError: grok-web bridge 404: {"detail":"grok.com 404:
      {\"error\":{\"code\":5,\"message\":\"'Conversation' with ID
      'X' was not found.\"…}}"}`
     — stale operator-configured conversation ID; 404 from grok.com.
     Pre-fix: ``unknown``. Post-fix: ``bad_request``.

  2. `GrokWebError: grok-web bridge unreachable: Server disconnected
      without sending a response.`
     — formatted prose for the httpx RemoteProtocolError shape (the
     existing pattern matched the exception NAME but not the prose
     surfaced when wrapped by grok_bridge.GrokWebError).
     Pre-fix: ``unknown``. Post-fix: ``network``.

These tests pin the exact prod-observed error strings + add coverage
for the broader 4xx-other-than-401/403/429 buckets we added
prophylactically.
"""
from __future__ import annotations

import pytest

from app.routing.circuit_breaker import classify_error


# ── BUG-048 specific: actual prod strings ────────────────────────


def test_grok_bridge_404_conversation_not_found():
    """4 of 5 recent unknown-class errors on Grok-Web-Devin were this
    exact shape (stale conversation ID at grok.com)."""
    err = (
        'GrokWebError: grok-web bridge 404: '
        '{"detail":"grok.com 404: {\\"error\\":{\\"code\\":5,'
        '\\"message\\":\\"\'Conversation\' with ID '
        '\'e41fca28-3df3-44ae-ad27-1cb65d5fe2a5\' was not found.\\","details\\":[]}}\\n"}'
    )
    assert classify_error(err) == "bad_request"


def test_grok_bridge_unreachable_remote_protocol_prose():
    """The 5th unknown was the httpx RemoteProtocolError prose
    surfaced via GrokWebError."""
    err = (
        "GrokWebError: grok-web bridge unreachable: "
        "Server disconnected without sending a response."
    )
    assert classify_error(err) == "network"


# ── broader 4xx coverage prophylactic ────────────────────────────


@pytest.mark.parametrize("code,phrase", [
    ("404", "Not Found"),
    ("405", "Method Not Allowed"),
    ("409", "Conflict"),
    ("410", "Gone"),
    ("413", "Payload Too Large"),
    ("415", "Unsupported Media Type"),
    ("422", "Unprocessable Entity"),
])
def test_4xx_codes_bucket_as_bad_request(code, phrase):
    assert classify_error(f"upstream returned {code}: {phrase}") == "bad_request"


def test_lowercase_not_found_phrase():
    """The phrase 'not found' alone (lowercase, no status code)
    should also bucket as bad_request — surfaces in grok.com's
    nested JSON without an HTTP code at that layer."""
    assert classify_error(
        "grok.com responded: object 'X' was not found"
    ) == "bad_request"


# ── network-pattern additions for httpx prose ────────────────────


def test_server_disconnected_prose_buckets_network():
    """The httpx RemoteProtocolError documented message — not just
    the exception name."""
    assert classify_error("Server disconnected") == "network"


def test_without_sending_a_response_prose():
    assert classify_error("upstream closed connection without sending a response") == "network"


def test_bridge_unreachable_grokwebkit_wrapper():
    """grok_bridge.GrokWebError wraps connection errors with this
    prefix; without the new pattern the inner classifier never
    sees the underlying httpx exception name."""
    assert classify_error("GrokWebError: grok-web bridge unreachable: foo") == "network"


# ── regressions: existing patterns must still resolve ────────────


def test_502_still_upstream_5xx():
    assert classify_error("upstream returned 502 Bad Gateway") == "upstream_5xx"


def test_503_still_upstream_5xx():
    assert classify_error("503 Service Unavailable") == "upstream_5xx"


def test_429_still_rate_limit():
    """429 must NOT match the new 4xx bad_request list — it's already
    a more-specific bucket (rate_limit) earlier in the chain."""
    assert classify_error("429 Too Many Requests") == "rate_limit"


def test_401_still_auth():
    """401 must NOT match bad_request — auth detection runs first."""
    assert classify_error("HTTP 401 invalid API key") == "auth"


def test_403_still_auth():
    assert classify_error("HTTP 403 Forbidden") == "auth"


def test_402_still_billing():
    assert classify_error("HTTP 402 Payment Required") == "billing"


def test_known_camelcase_still_network():
    """Don't regress on the v3.0.88 exception-name patterns."""
    assert classify_error("httpx.ReadError: …") == "network"
    assert classify_error("RemoteProtocolError: …") == "network"


def test_400_still_bad_request():
    assert classify_error("400 Bad Request: invalid model") == "bad_request"


def test_empty_string_still_unknown():
    assert classify_error("") == "unknown"


def test_genuine_unknown_stays_unknown():
    """A string with NO recognised pattern still buckets to unknown."""
    assert classify_error("something completely unexpected happened") == "unknown"


# ── ordering invariant ───────────────────────────────────────────


def test_auth_wins_over_404_when_both_present():
    """A 404 wrapped around an auth-class error should bucket as
    auth (more specific). The classifier walks
    auth → billing → rate_limit → timeout → network → 5xx →
    bad_request, so auth wins if its `is_auth_error()` matches first.
    Uses an actual AUTH_ERROR_PATTERNS phrase (`unauthorized`)."""
    err = "404 Not Found: unauthorized — invalid x-api-key"
    assert classify_error(err) == "auth"
