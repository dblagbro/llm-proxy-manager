"""Unit tests for router pure helpers (build_litellm_model, kwargs, native-thinking)."""
import sys
import types
import pytest

# Stub heavy deps before app imports
_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.routing.router import (
    build_litellm_model,
    build_litellm_kwargs,
    _native_thinking_params,
    PROVIDER_TYPE_TO_LITELLM,
    PROVIDER_DEFAULT_MODELS,
)


class _FakeProvider:
    """Minimal Provider stand-in — just the fields router helpers touch."""
    def __init__(
        self,
        provider_type="openai",
        default_model=None,
        api_key="sk-test",
        base_url=None,
        timeout_sec=30,
    ):
        self.provider_type = provider_type
        self.default_model = default_model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_sec = timeout_sec


# ── build_litellm_model ──────────────────────────────────────────────────────


class TestBuildLitellmModel:
    def test_anthropic_prefix(self):
        p = _FakeProvider(provider_type="anthropic", default_model="claude-sonnet-4-5")
        assert build_litellm_model(p) == "anthropic/claude-sonnet-4-5"

    def test_openai_prefix(self):
        p = _FakeProvider(provider_type="openai", default_model="gpt-4o")
        assert build_litellm_model(p) == "openai/gpt-4o"

    def test_google_translates_to_gemini(self):
        p = _FakeProvider(provider_type="google", default_model="gemini-2.0-flash")
        assert build_litellm_model(p) == "gemini/gemini-2.0-flash"

    def test_vertex_translates_to_vertex_ai(self):
        p = _FakeProvider(provider_type="vertex", default_model="gemini-2.0-flash-002")
        assert build_litellm_model(p) == "vertex_ai/gemini-2.0-flash-002"

    def test_grok_translates_to_xai(self):
        p = _FakeProvider(provider_type="grok", default_model="grok-2")
        assert build_litellm_model(p) == "xai/grok-2"

    def test_compatible_uses_openai_prefix(self):
        p = _FakeProvider(provider_type="compatible", default_model="llama-3.1-70b")
        assert build_litellm_model(p) == "openai/llama-3.1-70b"

    def test_override_wins_over_default_model(self):
        p = _FakeProvider(provider_type="openai", default_model="gpt-4o")
        assert build_litellm_model(p, model_override="gpt-4o-mini") == "openai/gpt-4o-mini"

    def test_falls_back_to_provider_default_when_no_model(self):
        """If provider has no default_model set, use the PROVIDER_DEFAULT_MODELS map."""
        p = _FakeProvider(provider_type="openai", default_model=None)
        assert build_litellm_model(p) == "openai/gpt-4o"

    def test_anthropic_falls_back(self):
        p = _FakeProvider(provider_type="anthropic", default_model=None)
        assert build_litellm_model(p) == "anthropic/claude-sonnet-4-6"

    def test_unknown_provider_type_defaults_to_openai_prefix(self):
        p = _FakeProvider(provider_type="some-new-type", default_model="whatever")
        # unknown → "openai" prefix, and the default for unknown is "gpt-4o"
        assert build_litellm_model(p) == "openai/whatever"


class TestProviderMaps:
    def test_all_known_types_have_default(self):
        for provider_type in PROVIDER_TYPE_TO_LITELLM:
            assert provider_type in PROVIDER_DEFAULT_MODELS, f"{provider_type} missing default"

    def test_compatible_points_at_openai(self):
        assert PROVIDER_TYPE_TO_LITELLM["compatible"] == "openai"

    def test_google_maps_to_gemini(self):
        assert PROVIDER_TYPE_TO_LITELLM["google"] == "gemini"


# ── build_litellm_kwargs ─────────────────────────────────────────────────────


class TestBuildLitellmKwargs:
    def test_api_key_included_when_set(self):
        p = _FakeProvider(api_key="sk-abc")
        k = build_litellm_kwargs(p)
        assert k["api_key"] == "sk-abc"

    def test_api_key_omitted_when_empty(self):
        p = _FakeProvider(api_key="")
        k = build_litellm_kwargs(p)
        assert "api_key" not in k

    def test_api_key_omitted_when_none(self):
        p = _FakeProvider(api_key=None)
        k = build_litellm_kwargs(p)
        assert "api_key" not in k

    def test_timeout_always_set(self):
        p = _FakeProvider(timeout_sec=45)
        k = build_litellm_kwargs(p)
        assert k["timeout"] == 45

    def test_base_url_included_for_ollama(self):
        p = _FakeProvider(provider_type="ollama", base_url="http://localhost:11434")
        k = build_litellm_kwargs(p)
        assert k["api_base"] == "http://localhost:11434"

    def test_base_url_included_for_compatible(self):
        p = _FakeProvider(provider_type="compatible", base_url="https://my-gateway/v1")
        k = build_litellm_kwargs(p)
        assert k["api_base"] == "https://my-gateway/v1"

    def test_base_url_ignored_for_openai(self):
        """OpenAI proper doesn't take api_base — the base_url field is stored but unused."""
        p = _FakeProvider(provider_type="openai", base_url="https://should-be-ignored/")
        k = build_litellm_kwargs(p)
        assert "api_base" not in k

    def test_base_url_ignored_for_anthropic(self):
        p = _FakeProvider(provider_type="anthropic", base_url="https://ignored/")
        k = build_litellm_kwargs(p)
        assert "api_base" not in k

    def test_missing_base_url_doesnt_crash_ollama(self):
        p = _FakeProvider(provider_type="ollama", base_url=None)
        k = build_litellm_kwargs(p)
        assert "api_base" not in k


# ── _native_thinking_params ──────────────────────────────────────────────────


class TestNativeThinkingParams:
    def test_gemini_25_enables_thinking(self):
        params = _native_thinking_params("google", "gemini-2.5-pro")
        assert params["thinking"]["type"] == "enabled"
        assert "budget_tokens" in params["thinking"]

    def test_gemini_25_vertex_also_enables(self):
        params = _native_thinking_params("vertex", "gemini-2.5-flash")
        assert params["thinking"]["type"] == "enabled"

    def test_gemini_20_does_not_enable_thinking(self):
        # Only 2.5 gets the thinking block
        params = _native_thinking_params("google", "gemini-2.0-flash")
        assert params == {}

    def test_openai_o1_enables_reasoning_effort(self):
        params = _native_thinking_params("openai", "o1-preview")
        assert "reasoning_effort" in params

    def test_openai_o3_enables_reasoning_effort(self):
        params = _native_thinking_params("openai", "o3-mini")
        assert "reasoning_effort" in params

    def test_openai_o4_enables_reasoning_effort(self):
        params = _native_thinking_params("openai", "o4-future")
        assert "reasoning_effort" in params

    def test_openai_gpt4_does_not_enable(self):
        """gpt-* is not in the o-series, shouldn't enable reasoning_effort."""
        params = _native_thinking_params("openai", "gpt-4o")
        assert params == {}

    def test_anthropic_returns_empty(self):
        """Anthropic thinking is handled at the response-body level, not kwargs."""
        params = _native_thinking_params("anthropic", "claude-opus-4")
        assert params == {}

    def test_unknown_provider_returns_empty(self):
        params = _native_thinking_params("mystery", "mystery-model")
        assert params == {}

    def test_o_series_regex_uppercase_skipped(self):
        """Regex is case-sensitive via lower(); O1 uppercase maps via .lower() → o1."""
        params = _native_thinking_params("openai", "O1-preview")
        assert "reasoning_effort" in params
