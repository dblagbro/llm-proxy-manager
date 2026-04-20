"""
Model capability scanner.
Queries each provider's model list API and auto-infers capability profiles.
"""
import logging
from typing import Optional

import httpx
import litellm

from app.models.db import Provider, ModelCapability
from app.routing.lmrh import infer_capability_profile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

logger = logging.getLogger(__name__)


async def scan_provider_models(db: AsyncSession, provider: Provider) -> list[dict]:
    """
    Fetch model list from provider, infer capability profiles, upsert to DB.
    Returns list of discovered model dicts.
    """
    models = await _fetch_model_list(provider)
    if not models:
        logger.warning(f"No models discovered for provider {provider.id}")
        return []

    # Delete existing inferred profiles (keep manual ones)
    await db.execute(
        delete(ModelCapability).where(
            ModelCapability.provider_id == provider.id,
            ModelCapability.source == "inferred",
        )
    )

    upserted = []
    for model_id in models:
        profile = infer_capability_profile(
            provider.id, provider.provider_type, model_id, provider.priority
        )
        cap = ModelCapability(
            provider_id=provider.id,
            model_id=model_id,
            tasks=profile.tasks,
            latency=profile.latency,
            cost_tier=profile.cost_tier,
            safety=profile.safety,
            context_length=profile.context_length,
            regions=profile.regions,
            modalities=profile.modalities,
            native_reasoning=profile.native_reasoning,
            source="inferred",
        )
        db.add(cap)
        upserted.append({
            "model_id": model_id,
            "tasks": profile.tasks,
            "cost_tier": profile.cost_tier,
            "native_reasoning": profile.native_reasoning,
        })

    await db.commit()
    logger.info(f"Scanned {len(upserted)} models for provider {provider.id}")
    return upserted


async def _fetch_model_list(provider: Provider) -> list[str]:
    """Fetch model IDs from provider API."""
    try:
        match provider.provider_type:
            case "anthropic":
                return await _fetch_anthropic_models(provider)
            case "openai" | "compatible" | "grok":
                return await _fetch_openai_models(provider)
            case "google":
                return await _fetch_google_models(provider)
            case "ollama":
                return await _fetch_ollama_models(provider)
            case "vertex":
                return _vertex_default_models()
            case _:
                return []
    except Exception as e:
        logger.warning(f"Model scan failed for {provider.id}: {e}")
        return []


async def _fetch_anthropic_models(provider: Provider) -> list[str]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": provider.api_key, "anthropic-version": "2023-06-01"},
        )
        resp.raise_for_status()
        data = resp.json()
        return [m["id"] for m in data.get("data", [])]


async def _fetch_openai_models(provider: Provider) -> list[str]:
    base = provider.base_url or "https://api.openai.com"
    if provider.provider_type == "grok":
        base = "https://api.x.ai"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{base.rstrip('/')}/v1/models",
            headers={"Authorization": f"Bearer {provider.api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return [m["id"] for m in data.get("data", [])]


async def _fetch_google_models(provider: Provider) -> list[str]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={provider.api_key}"
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            m["name"].replace("models/", "")
            for m in data.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]


async def _fetch_ollama_models(provider: Provider) -> list[str]:
    base = provider.base_url or "http://localhost:11434"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{base.rstrip('/')}/api/tags")
        resp.raise_for_status()
        data = resp.json()
        return [m["name"] for m in data.get("models", [])]


def _vertex_default_models() -> list[str]:
    return [
        "gemini-2.5-flash-002",
        "gemini-2.5-pro-002",
        "gemini-1.5-pro-002",
        "gemini-1.5-flash-002",
    ]


async def test_provider(provider: Provider) -> dict:
    """Send a minimal test request to verify provider is reachable."""
    import litellm
    from app.routing.router import _build_litellm_model, _build_litellm_kwargs
    from app.routing.circuit_breaker import record_failure, record_success, is_billing_error

    model = _build_litellm_model(provider)
    kwargs = _build_litellm_kwargs(provider)

    try:
        result = await litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": "Reply with: OK"}],
            max_tokens=5,
            stream=False,
            **kwargs,
        )
        text = result.choices[0].message.content or ""
        await record_success(provider.id)
        return {"success": True, "response": text, "model": model}
    except Exception as e:
        err_str = str(e)
        billing = is_billing_error(err_str)
        await record_failure(provider.id, billing_error=billing)
        return {
            "success": False,
            "error": err_str,
            "model": model,
            "billing_error": billing,
        }
