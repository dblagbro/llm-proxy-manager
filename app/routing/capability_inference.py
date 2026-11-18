"""
Capability inference — heuristic fallback when no DB record exists for a provider+model.

Separate from the LMRH scoring protocol (lmrh.py) because these two concerns evolve
independently: scoring logic changes with the LMRH spec; inference changes with new
model families. Adding support for a new model family means editing only this file.
"""
from app.routing.lmrh import CapabilityProfile


def infer_capability_profile(
    provider_id: str,
    provider_type: str,
    model_id: str,
    priority: int = 10,
) -> CapabilityProfile:
    """Infer capability profile from model name heuristics."""
    m = model_id.lower()
    profile = CapabilityProfile(
        provider_id=provider_id,
        provider_type=provider_type,
        model_id=model_id,
        priority=priority,
    )

    # Reasoning / native thinking detection
    if any(x in m for x in ["opus", "o1", "o3", "o4", "r1", "deepseek-r", "gemini-2.5", "claude-3-7"]):
        profile.native_reasoning = True
        profile.tasks = ["reasoning", "analysis", "code", "chat"]
        profile.cost_tier = "premium"
        profile.latency = "high"
        profile.safety = 4

    elif any(x in m for x in ["sonnet", "gpt-4o", "gemini-2.0", "gpt-4-turbo", "grok-2"]):
        profile.tasks = ["reasoning", "analysis", "code", "chat"]
        profile.cost_tier = "standard"
        profile.latency = "medium"
        profile.safety = 4

    elif any(x in m for x in ["haiku", "flash", "mini", "gpt-3.5", "grok-beta"]):
        profile.tasks = ["chat", "analysis"]
        profile.cost_tier = "economy"
        profile.latency = "low"
        profile.safety = 3

    # Vision
    if any(x in m for x in ["vision", "vl", "gpt-4o", "gemini", "claude-3", "llava"]):
        profile.modalities = ["text", "vision"]
        profile.native_vision = True

    # Native tool support
    # Compatible: unknown endpoint — default False to be safe.
    # Ollama: modern local models (Qwen3-Coder, Llama 3.1+, Mistral) expose
    # native tools via /api/chat. Default True so we don't force prompt
    # emulation; the tool prober can flip False if a specific model fails.
    # Everything else (Anthropic, OpenAI, Google, Grok, Vertex): True
    # (CapabilityProfile.native_tools already defaults True).
    if provider_type == "compatible":
        profile.native_tools = False

    # Provider-specific region defaults
    if provider_type == "google":
        profile.regions = ["us", "eu", "asia"]
    elif provider_type in ("anthropic", "openai", "grok", "grok-web", "claude-oauth", "ChatGPT-oauth-plan", "cohere", "azure"):
        profile.regions = ["us"]
    elif provider_type == "openrouter":
        # v3.1.3: OpenRouter is multi-region — they marketplace across vendors.
        # Use the union of common regions to avoid sovereignty filter rejecting
        # legitimate routes. Operator can override per-model via scan-models.
        profile.regions = ["us", "eu", "asia"]
    elif provider_type == "nvidia_nim":
        # v5.11.0: NVIDIA NIM hosts vendor models (NVIDIA's own + Z.ai +
        # MoonshotAI + MiniMax + ...). Multi-region. Operator can refine.
        profile.regions = ["us", "eu", "asia"]
    elif provider_type == "ollama":
        profile.regions = ["local"]
        profile.cost_tier = "economy"
        profile.latency = "medium"
        # Local GGUF context is RAM/VRAM-bound. The CapabilityProfile
        # default of 128k would pass _capability_fit and then OOM a
        # 6 GB / 32 GB laptop. 32k matches the reference envelope in
        # docs/5.23-local-accelerator-orchestration-backpressure-design.md §8.
        profile.context_length = 32768

    return profile
