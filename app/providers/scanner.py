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
            case "claude-oauth":
                return await _fetch_claude_oauth_models(provider)
            case "codex-oauth":
                return await _fetch_codex_oauth_models(provider)
            case "cohere":
                return await _fetch_cohere_models(provider)
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


async def _fetch_claude_oauth_models(provider: Provider) -> list[str]:
    """Claude Pro Max tokens can't use x-api-key; Bearer + CC beta flags only.

    v2.7.6: on 401, attempt one refresh-and-retry via refresh_and_persist.
    Caller (scan_provider_models) provides a DB session that we use to
    persist the rotated tokens. Falls back gracefully if the refresh fails."""
    from app.providers.claude_oauth import build_headers, PLATFORM_BASE_URL
    from app.providers.claude_oauth_flow import refresh_and_persist, OAuthFlowError
    from app.models.database import AsyncSessionLocal

    current_token = provider.api_key or ""
    refreshed = False
    while True:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"{PLATFORM_BASE_URL}/v1/models",
                headers=build_headers(current_token),
            )
        if resp.status_code == 401 and not refreshed and provider.oauth_refresh_token:
            try:
                async with AsyncSessionLocal() as db:
                    from sqlalchemy import select
                    p = (await db.execute(select(Provider).where(Provider.id == provider.id))).scalar_one()
                    result = await refresh_and_persist(p, db)
                    current_token = result.access_token
                    refreshed = True
                    continue
            except OAuthFlowError:
                pass
        resp.raise_for_status()
        data = resp.json()
        return [m["id"] for m in data.get("data", [])]


async def _fetch_codex_oauth_models(provider: Provider) -> list[str]:
    """Codex CLI / ChatGPT subscription model list.

    Hits ``chatgpt.com/backend-api/codex/models?client_version=...`` with
    the OAuth bearer + ``ChatGPT-Account-ID`` workspace header. The list
    is per-account-tier — Plus, Pro, Team and Enterprise see different
    slugs. ``client_version`` is required as a query parameter; the
    backend rejects requests without it.

    Mirrors the auto-refresh-on-401 retry from claude-oauth.
    """
    from app.providers.codex_oauth import (
        CODEX_MODELS_URL, CODEX_CLIENT_VERSION, build_headers,
    )
    from app.providers.codex_oauth_flow import refresh_and_persist, OAuthFlowError
    from app.models.database import AsyncSessionLocal

    cfg = provider.extra_config or {}
    account_id = cfg.get("chatgpt_account_id") if isinstance(cfg, dict) else None
    current_token = provider.api_key or ""
    refreshed = False
    while True:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{CODEX_MODELS_URL}?client_version={CODEX_CLIENT_VERSION}",
                # /models is plain JSON, NOT SSE — override the default Accept
                # that build_headers sets for the streaming /responses path.
                headers=build_headers(
                    current_token, chatgpt_account_id=account_id,
                    extra={"Accept": "application/json"},
                ),
            )
        if resp.status_code == 401 and not refreshed and provider.oauth_refresh_token:
            try:
                async with AsyncSessionLocal() as db:
                    from sqlalchemy import select
                    p = (await db.execute(select(Provider).where(Provider.id == provider.id))).scalar_one()
                    result = await refresh_and_persist(p, db)
                    current_token = result.access_token
                    refreshed = True
                    continue
            except OAuthFlowError:
                pass
        resp.raise_for_status()
        data = resp.json()
        return [m["slug"] for m in data.get("models", []) if m.get("slug")]


async def _fetch_cohere_models(provider: Provider) -> list[str]:
    """Cohere model list. Their /v1/models endpoint returns chat + embed +
    rerank models with an ``endpoints`` array indicating which surfaces
    each supports. We return all model ids; downstream routing filters
    by /v1/embeddings vs /v1/chat/completions surface based on
    model_capabilities.
    """
    base = provider.base_url or "https://api.cohere.com"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{base.rstrip('/')}/v1/models",
            headers={"Authorization": f"Bearer {provider.api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        # Cohere shape: {"models": [{"name": "embed-english-v3.0", "endpoints": [...]}]}
        return [m["name"] for m in data.get("models", []) if m.get("name")]


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
    if provider.provider_type == "codex-oauth":
        return await _test_codex_oauth(provider)

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

    # v3.0.31: same embedding-on-chat guard as the keepalive probe (v3.0.30).
    # `build_litellm_model(provider)` reads `provider.default_model`, and on
    # Cohere that's `embed-english-v3.0` — Cohere's chat API rejects it with
    # 400. The "Test" button on the providers admin tab kept producing this
    # error even on healthy providers because the test is a chat-shaped call.
    # Pick a chat-capable model from caps when available.
    from app.routing.router import _is_embedding_model
    if _is_embedding_model(provider.default_model or ""):
        from sqlalchemy import select
        from app.models.db import ModelCapability
        from app.models.database import AsyncSessionLocal
        async with AsyncSessionLocal() as _cap_db:
            caps = (await _cap_db.execute(
                select(ModelCapability.model_id).where(
                    ModelCapability.provider_id == provider.id
                )
            )).scalars().all()
        chat_candidates = [c for c in caps if not _is_embedding_model(c)]
        if chat_candidates:
            preferred = [c for c in chat_candidates
                         if c.startswith("command-") or c.startswith("gpt-")]
            chosen = (preferred or sorted(chat_candidates))[0]
            # Re-derive litellm model id with the override
            model = build_litellm_model(provider, model_override=chosen)
        else:
            return {
                "success": True,
                "response": (f"Skipped — provider's default model {provider.default_model!r} "
                             "is embeddings-only and no chat models are scanned. "
                             "Use POST /v1/embeddings to test the embeddings surface, "
                             "or scan models first."),
                "model": provider.default_model,
                "embedding_only": True,
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

    body = _inject_claude_code_system({
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Reply with: OK"}],
    })
    url = f"{PLATFORM_BASE_URL}/v1/messages?beta=true"

    # v2.7.6: refresh-and-retry once on 401 so admins see the real status
    from app.providers.claude_oauth_flow import refresh_and_persist, OAuthFlowError
    from app.models.database import AsyncSessionLocal
    current_token = provider.api_key
    refreshed = False
    try:
        while True:
            headers = {
                **build_headers(current_token, model=model),
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                r = await c.post(url, json=body, headers=headers)
            if r.status_code == 401 and not refreshed and provider.oauth_refresh_token:
                try:
                    async with AsyncSessionLocal() as db:
                        from sqlalchemy import select
                        p = (await db.execute(select(Provider).where(Provider.id == provider.id))).scalar_one()
                        result = await refresh_and_persist(p, db)
                        current_token = result.access_token
                        refreshed = True
                        continue
                except OAuthFlowError:
                    pass  # fall through to normal error path
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


async def _test_codex_oauth(provider: Provider) -> dict:
    """Smoke-test a codex-oauth provider against
    ``chatgpt.com/backend-api/codex/responses`` directly.

    The Codex backend requires ``stream: true`` (rejects non-stream calls
    with a 400), so we open a streaming POST and consume just enough
    events to confirm a clean ``response.completed`` arrives. Refreshes
    once on 401 via the rotated tokens, mirroring the claude-oauth path.
    """
    import httpx
    import json as _json
    from app.providers.codex_oauth import (
        CODEX_RESPONSES_URL, build_headers,
    )
    from app.providers.codex_oauth_flow import refresh_and_persist, OAuthFlowError
    from app.routing.circuit_breaker import (
        record_failure, record_success, is_billing_error,
    )
    from app.models.database import AsyncSessionLocal
    from sqlalchemy import select

    model = provider.default_model or "gpt-5.5"
    if not provider.api_key:
        return {
            "success": False,
            "error": "No OAuth access_token stored. Re-run the authorize flow.",
            "model": model,
            "billing_error": False,
            "hint": "missing_api_key",
        }

    cfg = provider.extra_config or {}
    account_id = cfg.get("chatgpt_account_id") if isinstance(cfg, dict) else None

    body = {
        "model": model,
        "instructions": "Reply briefly.",
        "input": [{
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Reply with: OK"}],
        }],
        "stream": True,
        "store": False,
    }

    current_token = provider.api_key
    refreshed = False
    try:
        while True:
            headers = build_headers(current_token, chatgpt_account_id=account_id)
            async with httpx.AsyncClient(timeout=30.0) as c:
                async with c.stream("POST", CODEX_RESPONSES_URL, headers=headers, json=body) as r:
                    if r.status_code == 401 and not refreshed and provider.oauth_refresh_token:
                        try:
                            async with AsyncSessionLocal() as db:
                                p = (await db.execute(select(Provider).where(Provider.id == provider.id))).scalar_one()
                                result = await refresh_and_persist(p, db)
                                current_token = result.access_token
                                refreshed = True
                                continue
                        except OAuthFlowError:
                            pass
                    if r.status_code >= 400:
                        err_body = (await r.aread()).decode(errors="replace")
                        err = f"{r.status_code}: {err_body[:400]}"
                        billing = is_billing_error(err)
                        await record_failure(provider.id, billing_error=billing)
                        return {
                            "success": False,
                            "error": err, "error_detail": err,
                            "model": model, "billing_error": billing,
                        }
                    text_parts: list[str] = []
                    async for line in r.aiter_lines():
                        if not line or line.startswith("event:") or not line.startswith("data:"):
                            continue
                        try:
                            evt = _json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        if evt.get("type") == "response.output_text.delta":
                            d = evt.get("delta")
                            if isinstance(d, str):
                                text_parts.append(d)
                        elif evt.get("type") == "response.completed":
                            await record_success(provider.id)
                            return {
                                "success": True,
                                "response": "".join(text_parts),
                                "model": model,
                            }
                    await record_failure(provider.id, billing_error=False)
                    return {
                        "success": False,
                        "error": "stream ended without response.completed",
                        "model": model, "billing_error": False,
                    }
    except httpx.HTTPError as e:
        err_str = str(e)
        await record_failure(provider.id, billing_error=False)
        return {
            "success": False,
            "error": err_str, "error_detail": err_str,
            "model": model, "billing_error": False,
        }
