"""Unit tests for capability inference heuristics."""
import sys
import types

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.routing.capability_inference import infer_capability_profile


class TestReasoningFamily:
    def test_opus_is_reasoning(self):
        p = infer_capability_profile("id", "anthropic", "claude-opus-4")
        assert p.native_reasoning is True
        assert "reasoning" in p.tasks
        assert p.cost_tier == "premium"
        assert p.safety == 4

    def test_o1_is_reasoning(self):
        p = infer_capability_profile("id", "openai", "o1-preview")
        assert p.native_reasoning is True
        assert p.latency == "high"

    def test_o3_is_reasoning(self):
        p = infer_capability_profile("id", "openai", "o3-mini")
        assert p.native_reasoning is True

    def test_deepseek_r1_is_reasoning(self):
        p = infer_capability_profile("id", "openai", "deepseek-r1")
        assert p.native_reasoning is True

    def test_gemini_25_is_reasoning(self):
        p = infer_capability_profile("id", "google", "gemini-2.5-pro")
        assert p.native_reasoning is True


class TestStandardFamily:
    def test_sonnet_is_standard(self):
        p = infer_capability_profile("id", "anthropic", "claude-sonnet-4")
        assert p.native_reasoning is False
        assert p.cost_tier == "standard"
        assert p.latency == "medium"

    def test_gpt_4o_is_standard(self):
        p = infer_capability_profile("id", "openai", "gpt-4o")
        assert p.cost_tier == "standard"

    def test_grok_2_is_standard(self):
        p = infer_capability_profile("id", "grok", "grok-2-latest")
        assert p.cost_tier == "standard"


class TestEconomyFamily:
    def test_haiku_is_economy(self):
        p = infer_capability_profile("id", "anthropic", "claude-haiku-4-5")
        assert p.cost_tier == "economy"
        assert p.latency == "low"
        assert p.safety == 3

    def test_flash_is_economy(self):
        # Use a gemini-flash name that doesn't contain "gemini-2.0" (standard tier)
        p = infer_capability_profile("id", "google", "gemini-flash-experimental")
        assert p.cost_tier == "economy"

    def test_gpt_35_is_economy(self):
        p = infer_capability_profile("id", "openai", "gpt-3.5-turbo")
        assert p.cost_tier == "economy"

    def test_grok_beta_is_economy(self):
        p = infer_capability_profile("id", "grok", "grok-beta")
        assert p.cost_tier == "economy"


class TestVision:
    def test_gpt_4o_has_vision(self):
        p = infer_capability_profile("id", "openai", "gpt-4o")
        assert p.native_vision is True
        assert "vision" in p.modalities

    def test_gemini_has_vision(self):
        p = infer_capability_profile("id", "google", "gemini-2.0-flash")
        assert p.native_vision is True

    def test_claude_3_has_vision(self):
        p = infer_capability_profile("id", "anthropic", "claude-3-5-sonnet")
        assert p.native_vision is True


class TestNativeTools:
    def test_ollama_has_no_native_tools(self):
        p = infer_capability_profile("id", "ollama", "llama3")
        assert p.native_tools is False

    def test_compatible_has_no_native_tools(self):
        p = infer_capability_profile("id", "compatible", "some-model")
        assert p.native_tools is False

    def test_anthropic_has_native_tools(self):
        p = infer_capability_profile("id", "anthropic", "claude-sonnet-4")
        assert p.native_tools is True

    def test_openai_has_native_tools(self):
        p = infer_capability_profile("id", "openai", "gpt-4o")
        assert p.native_tools is True


class TestRegions:
    def test_google_multi_region(self):
        p = infer_capability_profile("id", "google", "gemini-2.0-flash")
        assert set(p.regions) == {"us", "eu", "asia"}

    def test_anthropic_us_only(self):
        p = infer_capability_profile("id", "anthropic", "claude-sonnet-4")
        assert p.regions == ["us"]

    def test_ollama_local(self):
        p = infer_capability_profile("id", "ollama", "llama3")
        assert p.regions == ["local"]
        assert p.cost_tier == "economy"


class TestPriority:
    def test_custom_priority_preserved(self):
        p = infer_capability_profile("id", "openai", "gpt-4o", priority=1)
        assert p.priority == 1

    def test_default_priority(self):
        p = infer_capability_profile("id", "openai", "gpt-4o")
        assert p.priority == 10
