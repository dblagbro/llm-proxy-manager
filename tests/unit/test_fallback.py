"""Unit tests for ordered-fallback detection (Wave 3 #17)."""
import sys
import types
import pytest

sys.modules.setdefault("litellm", types.ModuleType("litellm"))

from app.routing.fallback import is_same_provider_retriable, FallbackChain


class TestRetriableDetection:
    def test_timeout_is_same_provider_retriable(self):
        exc = Exception("litellm.Timeout: upstream slow")
        assert is_same_provider_retriable(exc) is True

    def test_rate_limit_is_retriable(self):
        exc = Exception("litellm.RateLimitError: 429")
        assert is_same_provider_retriable(exc) is True

    def test_api_connection_error_is_retriable(self):
        exc = Exception("litellm.APIConnectionError: network down briefly")
        assert is_same_provider_retriable(exc) is True

    def test_internal_server_error_is_retriable(self):
        exc = Exception("litellm.InternalServerError: upstream 500")
        assert is_same_provider_retriable(exc) is True

    def test_auth_error_triggers_fallback(self):
        exc = Exception("litellm.AuthenticationError: invalid api key")
        assert is_same_provider_retriable(exc) is False

    def test_bad_request_triggers_fallback(self):
        exc = Exception("litellm.BadRequestError: context length exceeded")
        assert is_same_provider_retriable(exc) is False

    def test_unknown_error_triggers_fallback(self):
        exc = Exception("something completely different")
        assert is_same_provider_retriable(exc) is False


class TestFallbackChain:
    def test_empty_header(self):
        assert FallbackChain().as_header() == ""

    def test_single_ok(self):
        c = FallbackChain()
        c.add("Anthropic", "ok")
        assert c.as_header() == "Anthropic:ok"

    def test_multi_chain(self):
        c = FallbackChain()
        c.add("Anthropic", "err:AuthenticationError")
        c.add("OpenAI", "err:TimeoutError")
        c.add("Google", "ok")
        assert c.as_header() == "Anthropic:err:AuthenticationError,OpenAI:err:TimeoutError,Google:ok"
        assert len(c.attempts) == 3
