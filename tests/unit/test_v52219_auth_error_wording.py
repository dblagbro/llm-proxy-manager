"""v5.22.19 — "Incorrect API key provided" must classify as auth, not bad_request.

``AUTH_ERROR_PATTERNS`` contained ``"invalid api key"`` but not
``"incorrect api key"`` — which is the wording OpenAI and xAI actually use.
That one-word gap had large consequences, because the auth classification is
what gates the whole escalation chain:

    is_auth_error() False
      -> classify_error() returns "bad_request"
      -> record_auth_failure() never called
      -> _persist_auto_skip() never runs
      -> auto_skip_until never set
      -> generic 120s hold-down instead of 24h + "Needs re-auth" in the UI
      -> provider flaps forever

Measured on Devin-Codex-Gmail: **4,457 requests, 1 success, over 24 days**,
with ``auto_skip_until`` frozen at 2026-08-20 and **104 of 320**
``/v1/messages`` requests hard-failing 400 in one day. The v5.22.16
consecutive-streak fix could not help, because the code path that consults it
was never entered.

Not xAI-specific: OpenAI uses the same phrasing, so any OpenAI-family provider
with a dead key shared the blind spot.
"""

import pytest

from app.routing.circuit_breaker import classify_error, is_auth_error

# The exact string from production logs, 2026-09-05 through 2026-09-17.
XAI_PRODUCTION = (
    'litellm.BadRequestError: XaiException - {"code":"invalid-argument",'
    '"error":"Incorrect API key provided. You can obtain an API key from '
    'https://console.x.ai."}'
)


class TestDeadCredentialsClassifyAsAuth:
    @pytest.mark.parametrize(
        "label,text",
        [
            ("xai-production", XAI_PRODUCTION),
            (
                "openai-prose",
                "AuthenticationError: Incorrect API key provided: sk-abc***. "
                "You can find your API key at https://platform.openai.com/account/api-keys",
            ),
            ("openai-code", 'openai.AuthenticationError: {"error":{"code":"invalid_api_key"}}'),
            ("gemini", "API key not valid. Please pass a valid API key."),
        ],
    )
    def test_is_auth_error(self, label, text):
        assert is_auth_error(text) is True, f"{label} not recognised as an auth failure"

    @pytest.mark.parametrize(
        "label,text",
        [
            ("xai-production", XAI_PRODUCTION),
            ("gemini", "API key not valid. Please pass a valid API key."),
        ],
    )
    def test_classifies_as_auth_not_bad_request(self, label, text):
        """The bucket is what gates escalation, so assert on it directly."""
        assert classify_error(text) == "auth", f"{label} still buckets as bad_request"


class TestNoOverMatching:
    """A pattern list this broad is one careless entry away from classifying
    every client error as a dead credential, which would 24h-skip healthy
    providers. These are the cases that must stay out."""

    @pytest.mark.parametrize(
        "label,text,expected",
        [
            (
                "context-window",
                "litellm.ContextWindowExceededError: This model's maximum context "
                "length is 128000 tokens",
                "bad_request",
            ),
            (
                "genuine-bad-request",
                "BadRequestError: invalid-argument: messages must be non-empty",
                "bad_request",
            ),
            ("rate-limit", "RateLimitError: rate_limit_exceeded", "rate_limit"),
            ("timeout", "litellm.Timeout: Request timed out", "timeout"),
        ],
    )
    def test_not_misclassified_as_auth(self, label, text, expected):
        assert is_auth_error(text) is False, f"{label} wrongly flagged as auth"
        assert classify_error(text) == expected

    def test_empty_input_is_safe(self):
        assert is_auth_error("") is False
        assert is_auth_error(None) is False
        assert classify_error("") == "unknown"


class TestTheOriginalWordingStillWorks:
    """The entries that were already there must keep matching."""

    @pytest.mark.parametrize(
        "text",
        [
            "authentication_error",
            "invalid x-api-key",
            "invalid api key",
            "unauthorized",
            "permission_denied",
            "invalid_grant",
            "the api_key client option must be set",
        ],
    )
    def test_preexisting_patterns_intact(self, text):
        assert is_auth_error(text) is True
