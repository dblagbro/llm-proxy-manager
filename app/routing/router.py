"""
Provider router — selects the best available provider+model for a request.
Integrates circuit breaker, LMRH hint scoring, and CoT-E auto-engagement.
"""
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Any

import litellm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models.db import Provider, ModelCapability
from app.routing.circuit_breaker import is_available, record_success, record_failure, is_billing_error
from app.routing.lmrh import (
    LMRHHint, CapabilityProfile, rank_candidates, infer_capability_profile, build_capability_header
)

logger = logging.getLogger(__name__)


_O_SERIES = re.compile(r"^o[0-9]")


@dataclass
class RouteResult:
    provider: Provider
    profile: CapabilityProfile
    litellm_model: str          # e.g. "anthropic/claude-sonnet-4-5" or "openai/gpt-4o"
    litellm_kwargs: dict
    unmet_hints: list[str]
    cot_engaged: bool
    tool_emulation_engaged: bool
    capability_header: str
    native_thinking_params: dict = field(default_factory=dict)


def _native_thinking_params(provider_type: str, model_id: str) -> dict:
    """Return provider-specific reasoning kwargs to inject when native_reasoning=True."""
    m = model_id.lower()
    if provider_type in ("google", "vertex") and "2.5" in m:
        return {"thinking": {"type": "enabled", "budget_tokens": settings.native_thinking_budget_tokens}}
    if provider_type == "openai" and _O_SERIES.match(m):
        return {"reasoning_effort": settings.native_reasoning_effort}
    return {}


PROVIDER_TYPE_TO_LITELLM = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "gemini",
    "vertex": "vertex_ai",
    "ollama": "ollama",
    "grok": "xai",
    "compatible": "openai",     # OpenAI-compatible uses openai provider with custom base_url
}


PROVIDER_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "google":    "gemini-2.0-flash",
    "vertex":    "gemini-2.0-flash-002",
    "openai":    "gpt-4o",
    "grok":      "grok-2",
    "ollama":    "llama3",
    "compatible": "gpt-4o",
}


def _build_litellm_model(provider: Provider) -> str:
    prefix = PROVIDER_TYPE_TO_LITELLM.get(provider.provider_type, "openai")
    default = PROVIDER_DEFAULT_MODELS.get(provider.provider_type, "gpt-4o")
    model = provider.default_model or default
    return f"{prefix}/{model}"


def _build_litellm_kwargs(provider: Provider) -> dict:
    kwargs: dict[str, Any] = {}
    if provider.api_key:
        kwargs["api_key"] = provider.api_key
    if provider.base_url and provider.provider_type in ("ollama", "compatible"):
        kwargs["api_base"] = provider.base_url
    kwargs["timeout"] = provider.timeout_sec
    return kwargs


async def _load_profile(db: AsyncSession, provider: Provider) -> CapabilityProfile:
    """Load capability profile from DB, or infer from model name."""
    model_id = provider.default_model or ""
    result = await db.execute(
        select(ModelCapability).where(
            ModelCapability.provider_id == provider.id,
            ModelCapability.model_id == model_id,
        )
    )
    cap = result.scalar_one_or_none()
    if cap:
        return CapabilityProfile(
            provider_id=provider.id,
            provider_type=provider.provider_type,
            model_id=model_id,
            tasks=cap.tasks or ["chat"],
            latency=cap.latency or "medium",
            cost_tier=cap.cost_tier or "standard",
            safety=cap.safety or 3,
            context_length=cap.context_length or 128000,
            regions=cap.regions or [],
            modalities=cap.modalities or ["text"],
            native_reasoning=cap.native_reasoning or False,
            native_tools=cap.native_tools if cap.native_tools is not None else True,
            priority=provider.priority,
        )
    return infer_capability_profile(provider.id, provider.provider_type, model_id, provider.priority)


async def select_provider(
    db: AsyncSession,
    hint: Optional[LMRHHint],
    has_tools: bool = False,
    has_images: bool = False,
    key_type: str = "standard",
) -> RouteResult:
    """
    Select the best available provider+model for this request.
    Raises RuntimeError if no providers are available.
    """
    result = await db.execute(
        select(Provider).where(Provider.enabled == True).order_by(Provider.priority)
    )
    providers = result.scalars().all()

    if not providers:
        raise RuntimeError("No providers configured")

    # Filter available (circuit breaker + hold-down)
    available = [p for p in providers if await is_available(p.id)]
    if not available:
        raise RuntimeError("All providers are currently unavailable (circuit breakers open)")

    # Hard-block providers explicitly excluded from tool requests
    # (exclude_from_tool_requests=True means "never, even with emulation")
    if has_tools:
        available = [p for p in available if not p.exclude_from_tool_requests]
    if has_tools and not available:
        raise RuntimeError("No providers available for tool requests (all excluded)")

    # Load capability profiles
    profiles = [await _load_profile(db, p) for p in available]
    provider_map = {p.id: p for p in available}

    # LMRH ranking
    ranked = rank_candidates(profiles, hint)
    if not ranked:
        raise RuntimeError("No providers satisfy the required routing constraints (LLM-Hint hard constraints)")

    best_profile, unmet = ranked[0]
    provider = provider_map[best_profile.provider_id]

    # CoT-E auto-engagement:
    # Triggered when key_type=claude-code OR LLM-Hint task=reasoning + native_reasoning=false
    # Can be disabled globally via the cot_enabled runtime setting.
    cot_engaged = False
    cot_globally_enabled = getattr(settings, "cot_enabled", True)
    if cot_globally_enabled and not best_profile.native_reasoning:
        task_hint = hint.get("task") if hint else None
        if key_type == "claude-code" or (task_hint and task_hint.value == "reasoning"):
            cot_engaged = True

    litellm_model = _build_litellm_model(provider)
    litellm_kwargs = _build_litellm_kwargs(provider)

    native_params: dict = {}
    if best_profile.native_reasoning and not cot_engaged:
        native_params = _native_thinking_params(provider.provider_type, best_profile.model_id)

    tool_emulation = has_tools and not best_profile.native_tools and not cot_engaged

    cap_header = build_capability_header(best_profile, unmet, cot_engaged, tool_emulation)

    logger.info(
        "router.selected",
        extra={
            "provider": provider.id,
            "model": litellm_model,
            "cot": cot_engaged,
            "unmet": unmet,
        },
    )

    return RouteResult(
        provider=provider,
        profile=best_profile,
        litellm_model=litellm_model,
        litellm_kwargs=litellm_kwargs,
        unmet_hints=unmet,
        cot_engaged=cot_engaged,
        tool_emulation_engaged=tool_emulation,
        capability_header=cap_header,
        native_thinking_params=native_params,
    )
