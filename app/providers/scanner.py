"""
Model capability scanner.
Queries each provider's model list API and auto-infers capability profiles.
"""
import logging
from typing import Optional

import httpx
import litellm

from app.models.db import Provider, ModelCapability
from app.routing.capability_inference import infer_capability_profile
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
    from app.routing.router import build_litellm_model, build_litellm_kwargs
    from app.routing.circuit_breaker import record_failure, record_success, is_billing_error

    # v2.7.2: claude-oauth bypasses litellm entirely — OAuth tokens need
    # Bearer auth + specific beta flags + the CC system-prompt marker that
    # litellm doesn't know about. Use the same direct httpx path
    # /v1/messages uses in production.
    if provider.provider_type == "claude-oauth":
        return await _test_claude_oauth(provider)

    model = build_litellm_model(provider)
    kwargs = build_litellm_kwargs(provider)

    # Pre-flight: catch the common "no API key configured" case before we hit
    # litellm and get a 600-char Python traceback back. Anthropic/OpenAI/Grok
    # store the key on the provider row; ollama and compatible can be keyless.
    if provider.provider_type in ("anthropic", "openai", "google", "vertex", "grok", "cohere", "mistral", "groq", "together", "fireworks") and not provider.api_key:
        return {
            "success": False,
            "error": f"No API key configured for this {provider.provider_type} provider. Open the Edit modal and paste a key.",
            "model": model,
            "billing_error": False,
            "hint": "missing_api_key",
        }

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
        # Trim litellm's traceback to the first informative line for UI display
        short_err = err_str.split("\nTraceback", 1)[0].strip()
        if len(short_err) > 500:
            short_err = short_err[:500] + "…"
        await record_failure(provider.id, billing_error=billing)
        return {
            "success": False,
            "error": short_err,
            "error_detail": err_str,  # full trace still available to power-users
            "model": model,
            "billing_error": billing,
        }


async def _test_claude_oauth(provider: Provider) -> dict:
    """Smoke-test a claude-oauth provider against platform.claude.com directly.

    Mirrors what ``_complete_claude_oauth`` does in the real messages path —
    Bearer auth, CC beta flags, and the required system-prompt marker.
    """
    import httpx
    from app.providers.claude_oauth import build_headers, PLATFORM_BASE_URL
    from app.api._messages_streaming import _inject_claude_code_system
    from app.routing.circuit_breaker import record_failure, record_success, is_billing_error

    model = provider.default_model or "claude-sonnet-4-6"

    if not provider.api_key:
        return {
            "success": False,
            "error": "No OAuth access_token stored. Re-run the authorize flow.",
            "model": model,
            "billing_error": False,
            "hint": "missing_api_key",
        }

    headers = {**build_headers(provider.api_key), "Content-Type": "application/json"}
    body = _inject_claude_code_system({
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Reply with: OK"}],
    })
    url = f"{PLATFORM_BASE_URL}/v1/messages?beta=true"

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
            r = await c.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            err = f"{r.status_code}: {r.text[:400]}"
            billing = is_billing_error(err)
            await record_failure(provider.id, billing_error=billing)
            return {
                "success": False,
                "error": err,
                "error_detail": err,
                "model": model,
                "billing_error": billing,
            }
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        await record_success(provider.id)
        return {"success": True, "response": text, "model": model}
    except httpx.HTTPError as e:
        err_str = str(e)
        await record_failure(provider.id, billing_error=False)
        return {
            "success": False,
            "error": err_str,
            "error_detail": err_str,
            "model": model,
            "billing_error": False,
        }
