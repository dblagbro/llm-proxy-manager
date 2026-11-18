"""v5.23.0 — local-provider defaults that survive a cold GGUF load.

Pins the three traps in docs/5.23-local-accelerator-orchestration-backpressure-design.md
§1.2 / §3 gap 5–6:

- timeout_sec of 30s (column/API) or 60s (ProviderForm) on an Ollama row
  must not be handed to litellm — a 30–90s 30B load opens the breaker.
- capability inference must not force native_tools=False for every Ollama
  provider (that engaged tool emulation instead of Qwen3-Coder native tools).
- PROVIDER_DEFAULT_MODELS['ollama'] must not be the retired llama3 tag.
"""
from __future__ import annotations

import types

from app.routing.aliases import (
    HOSTED_DEFAULT_TIMEOUT_SEC,
    SELF_HOSTED_DEFAULT_TIMEOUT_SEC,
    coerce_self_hosted_timeout,
    default_timeout_sec_for_type,
    effective_timeout_sec,
)
from app.routing.capability_inference import infer_capability_profile
from app.routing.litellm_binding import PROVIDER_DEFAULT_MODELS, build_litellm_kwargs, build_litellm_model


class _Prov:
    def __init__(self, provider_type="ollama", timeout_sec=30, extra_config=None, owner_company=None):
        self.provider_type = provider_type
        self.timeout_sec = timeout_sec
        self.extra_config = extra_config or {}
        self.owner_company = owner_company
        self.api_key = None
        self.base_url = "http://127.0.0.1:11434"
        self.default_model = None


class TestTimeoutDefaults:
    def test_self_hosted_types_default_to_240(self):
        for t in ("ollama", "vllm", "llamacpp", "lmstudio", "localai"):
            assert default_timeout_sec_for_type(t) == 240

    def test_hosted_types_stay_at_30(self):
        for t in ("openai", "anthropic", "grok", "openrouter"):
            assert default_timeout_sec_for_type(t) == HOSTED_DEFAULT_TIMEOUT_SEC

    def test_legacy_30_on_ollama_lifts_to_240(self):
        assert effective_timeout_sec(_Prov(timeout_sec=30)) == SELF_HOSTED_DEFAULT_TIMEOUT_SEC

    def test_form_default_60_on_ollama_lifts_to_240(self):
        assert effective_timeout_sec(_Prov(timeout_sec=60)) == SELF_HOSTED_DEFAULT_TIMEOUT_SEC

    def test_explicit_90_on_ollama_is_kept(self):
        assert effective_timeout_sec(_Prov(timeout_sec=90)) == 90

    def test_explicit_15_on_ollama_is_kept(self):
        assert effective_timeout_sec(_Prov(timeout_sec=15)) == 15

    def test_hosted_30_is_not_lifted(self):
        assert effective_timeout_sec(_Prov(provider_type="openai", timeout_sec=30)) == 30

    def test_compatible_opt_in_self_hosted_lifts(self):
        p = _Prov(provider_type="compatible", timeout_sec=30, extra_config={"self_hosted": True})
        assert effective_timeout_sec(p) == SELF_HOSTED_DEFAULT_TIMEOUT_SEC

    def test_coerce_persists_240_for_legacy_create(self):
        assert coerce_self_hosted_timeout("ollama", 30) == 240
        assert coerce_self_hosted_timeout("ollama", 60) == 240
        assert coerce_self_hosted_timeout("ollama", 300) == 300
        assert coerce_self_hosted_timeout("openai", 30) == 30

    def test_build_litellm_kwargs_uses_lifted_timeout(self):
        k = build_litellm_kwargs(_Prov(timeout_sec=30))
        assert k["timeout"] == 240

    def test_build_litellm_kwargs_hosted_unchanged(self):
        k = build_litellm_kwargs(_Prov(provider_type="openai", timeout_sec=30, extra_config={}))
        assert k["timeout"] == 30


class TestOllamaDefaultModel:
    def test_default_is_not_llama3(self):
        assert PROVIDER_DEFAULT_MODELS["ollama"] != "llama3"

    def test_default_is_qwen_coder(self):
        assert PROVIDER_DEFAULT_MODELS["ollama"] == "qwen2.5-coder:7b"

    def test_build_litellm_model_falls_back_to_qwen_coder(self):
        p = types.SimpleNamespace(
            provider_type="ollama",
            default_model=None,
        )
        assert build_litellm_model(p) == "ollama/qwen2.5-coder:7b"


class TestOllamaNativeTools:
    def test_qwen_coder_native_tools(self):
        p = infer_capability_profile("id", "ollama", "qwen2.5-coder:7b")
        assert p.native_tools is True

    def test_qwen3_coder_30b_native_tools(self):
        p = infer_capability_profile("id", "ollama", "qwen3-coder:30b")
        assert p.native_tools is True
        assert p.context_length == 32768

    def test_compatible_still_false(self):
        p = infer_capability_profile("id", "compatible", "some-model")
        assert p.native_tools is False
