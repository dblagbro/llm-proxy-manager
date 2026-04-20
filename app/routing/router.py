"""
Provider router — selects the best available provider+model for a request.
Integrates circuit breaker, LMRH hint scoring, and CoT-E auto-engagement.
"""
import logging
import time
from dataclasses import dataclass
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


@dataclass
class RouteResult:
    provider: Provider
    profile: CapabilityProfile
    litellm_model: str          # e.g. "anthropic/claude-sonnet-4-5" or "openai/gpt-4o"
    litellm_kwargs: dict
    unmet_hints: list[str]
    cot_engaged: bool
    capability_header: str


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

    # Filter tool-incompatible providers
    if has_tools:
        available = [p for p in available if not p.exclude_from_tool_requests]
    if not available:
        raise RuntimeError("No providers available that support tool requests")

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
    cot_engaged = False
    if not best_profile.native_reasoning:
        task_hint = hint.get("task") if hint else None
        if key_type == "claude-code" or (task_hint and task_hint.value == "reasoning"):
            cot_engaged = True

    litellm_model = _build_litellm_model(provider)
    litellm_kwargs = _build_litellm_kwargs(provider)

    cap_header = build_capability_header(best_profile, unmet, cot_engaged)

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
        capability_header=cap_header,
    )
