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

    def test_ollama_default_is_not_llama3(self):
        """v5.23.0 — llama3 is no longer an honest Ollama catalog default."""
        assert PROVIDER_DEFAULT_MODELS["ollama"] == "qwen2.5-coder:7b"
        assert PROVIDER_DEFAULT_MODELS["ollama"] != "llama3"

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

    def test_ollama_legacy_timeout_is_lifted_to_240(self):
        """v5.23.0 — a 30s (column default) or 60s (form default) Ollama
        row must not be handed to litellm as-is; a 30–90s GGUF cold load
        would open the circuit breaker."""
        for legacy in (30, 60, None):
            p = _FakeProvider(provider_type="ollama", timeout_sec=legacy)
            k = build_litellm_kwargs(p)
            assert k["timeout"] == 240, f"legacy timeout {legacy!r} should lift to 240"

    def test_ollama_explicit_timeout_is_kept(self):
        p = _FakeProvider(provider_type="ollama", timeout_sec=300)
        k = build_litellm_kwargs(p)
        assert k["timeout"] == 300

    def test_hosted_timeout_is_not_lifted(self):
        p = _FakeProvider(provider_type="openai", timeout_sec=30)
        k = build_litellm_kwargs(p)
        assert k["timeout"] == 30

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

    # v4.4.35 regression — the cursor-oauth onboarding mystery (v4.4.31..v4.4.34)
    # was litellm getting model=openai/<x> + api_key=user_…::eyJ… but NO api_base,
    # defaulting to api.openai.com, and OpenAI rejecting the Cursor token verbatim
    # ("Incorrect API key provided: user_01J***…"). The Test button showed the
    # same litellm error on every reonboarding attempt; the actual token was
    # always fine. The fix added cursor-oauth to the api_base allowlist in
    # build_litellm_kwargs. These tests pin both halves of the wiring so a
    # future refactor can't silently regress dispatch.
    def test_base_url_included_for_cursor_oauth(self):
        p = _FakeProvider(
            provider_type="cursor-oauth",
            base_url="http://llm-proxy2-cursor-bridge:3010/v1",
            api_key="user_01ABC::eyJhbGciOiJIUzI1NiI.fake.signature",
        )
        k = build_litellm_kwargs(p)
        assert k["api_base"] == "http://llm-proxy2-cursor-bridge:3010/v1", (
            "cursor-oauth MUST set api_base; otherwise litellm sends the "
            "request to api.openai.com and OpenAI rejects the Cursor token. "
            "If this assertion fires after a router refactor, restore "
            'cursor-oauth in the api_base allowlist next to "ollama"/"compatible".'
        )
        assert k["api_key"].startswith("user_01ABC")

    def test_cursor_oauth_model_prefix_is_openai(self):
        """The sidecar speaks OpenAI Chat Completions; cursor-oauth's
        litellm model string therefore needs the openai/ prefix. If this
        ever changes (e.g. someone adds a dedicated 'cursor/' provider
        to litellm and we want to use it), update both
        PROVIDER_TYPE_TO_LITELLM and PROVIDER_DEFAULT_MODELS together."""
        assert PROVIDER_TYPE_TO_LITELLM["cursor-oauth"] == "openai"
        p = _FakeProvider(
            provider_type="cursor-oauth",
            default_model="claude-4-sonnet",
            base_url="http://llm-proxy2-cursor-bridge:3010/v1",
        )
        assert build_litellm_model(p) == "openai/claude-4-sonnet"

    def test_cursor_oauth_dispatch_passes_api_base_to_litellm(self):
        """End-to-end-ish: build_litellm_model + build_litellm_kwargs
        together produce the exact litellm.acompletion call the Test
        endpoint makes. Asserts the call would hit the sidecar, not
        api.openai.com.

        This is the test that would have caught v4.4.31..v4.4.34's
        Test-failure mystery 30 seconds after writing it."""
        p = _FakeProvider(
            provider_type="cursor-oauth",
            default_model="claude-4-sonnet",
            api_key="user_01ABC::eyJfake",
            base_url="http://llm-proxy2-cursor-bridge:3010/v1",
            timeout_sec=60,
        )
        model = build_litellm_model(p)
        kwargs = build_litellm_kwargs(p)
        # The model/api_base pair litellm receives:
        assert model == "openai/claude-4-sonnet"
        assert kwargs["api_base"] == "http://llm-proxy2-cursor-bridge:3010/v1"
        assert kwargs["api_key"] == "user_01ABC::eyJfake"
        assert kwargs["timeout"] == 60
        # Negative assertion: without api_base, litellm would default to
        # https://api.openai.com which would reject the user_… token.
        # If api_base disappears from kwargs in a future refactor, this
        # test must fail BEFORE the operator sees Test failures.
        assert "api_base" in kwargs

    def test_cursor_oauth_subscription_tier_membership(self):
        """Cost accounting: cursor-oauth must be in the subscription tier
        set so per-request cost_usd is recorded as 0 against the api_key's
        budget. Without this, the proxy bills traffic as if the upstream
        was charging per token (which it isn't — the operator's Cursor Pro
        subscription is flat-rate)."""
        from app.monitoring.helpers import SUBSCRIPTION_TIER_PROVIDER_TYPES
        assert "cursor-oauth" in SUBSCRIPTION_TIER_PROVIDER_TYPES


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
