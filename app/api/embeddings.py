"""
POST /v1/embeddings — OpenAI-compatible embeddings dispatch (v3.0.23, Q2).

Accepts the standard OpenAI embeddings request shape:

    {
      "model": "text-embedding-3-small",   # or cohere/embed-english-v3.0, etc.
      "input": "string"  | ["s1", "s2", ...],
      "encoding_format": "float"  | "base64",        # optional
      "dimensions": 1536,                            # optional, OpenAI v3+
      "user": "..."                                  # optional, ignored
    }

Routing: same ``select_provider`` machinery as chat completions, with the
v3.0.22 "model_capabilities filter" doing the heavy lifting — only providers
whose scanned capability rows include the requested embedding model are
eligible. Providers with no scan get a chance to try (and CB will catch
failures). Provider type is irrelevant to selection — any provider that
supports the model can serve it (so a Cohere provider is a sibling of an
OpenAI provider, not a separate path).

Dispatch: litellm.aembedding handles the per-vendor wire details (OpenAI,
Cohere, Google text-embedding-gecko, Azure, etc.). claude-oauth and
codex-oauth are excluded — neither subscription endpoint exposes embeddings.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import litellm
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.keys import resolve_api_key_dep
from app.models.database import get_db
from app.routing.router import select_provider, build_litellm_kwargs

logger = logging.getLogger(__name__)
router = APIRouter(tags=["embeddings"])


_AUTH = resolve_api_key_dep()


@router.post("/v1/embeddings")
async def create_embeddings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    key_record=Depends(_AUTH),
):
    body = await request.json()
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(400, "Missing required field: model")
    input_data = body.get("input")
    if input_data is None:
        raise HTTPException(400, "Missing required field: input")

    # Route. Subscription-OAuth providers don't expose embeddings; exclude.
    try:
        route = await select_provider(
            db, hint=None, has_tools=False, has_images=False,
            key_type=key_record.key_type,
            pinned_provider_id=None, model_override=model,
            sort_mode=None,
            excluded_provider_types={"claude-oauth", "codex-oauth"},
        )
    except RuntimeError as e:
        msg = str(e)
        raise HTTPException(
            503,
            f"No embeddings provider available for model {model!r}: {msg}. "
            "Add an openai/cohere/google provider whose scanned capabilities "
            "include this model, or pick a different model.",
        )

    provider = route.provider
    kwargs = build_litellm_kwargs(provider)

    # Build the litellm-shape model id. For embeddings we trust
    # build_litellm_model's prefix logic — it uses the provider type's
    # litellm prefix (cohere/, openai/, gemini/, etc.).
    from app.routing.router import build_litellm_model
    litellm_model = build_litellm_model(provider, model_override=model)

    extra: dict[str, Any] = {}
    if "encoding_format" in body:
        extra["encoding_format"] = body["encoding_format"]
    if "dimensions" in body:
        extra["dimensions"] = body["dimensions"]
    # Cohere uses ``input_type`` for embedding semantics; pass through if set.
    if "input_type" in body:
        extra["input_type"] = body["input_type"]

    t0 = time.monotonic()
    try:
        result = await litellm.aembedding(
            model=litellm_model, input=input_data, **kwargs, **extra,
        )
    except Exception as e:
        from app.routing.circuit_breaker import record_failure, is_billing_error
        err_str = str(e)
        billing = is_billing_error(err_str)
        await record_failure(provider.id, billing_error=billing)
        short = err_str.split("\nTraceback", 1)[0].strip()
        if len(short) > 500:
            short = short[:500] + "…"
        raise HTTPException(502, f"Embeddings upstream error: {short}")

    # Log + bump CB success. The result is litellm's EmbeddingResponse
    # which has model_dump() — fall back to dict shape if not.
    from app.routing.circuit_breaker import record_success
    await record_success(provider.id)

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    if hasattr(result, "model_dump"):
        body_out = result.model_dump()
    elif hasattr(result, "dict"):
        body_out = result.dict()
    else:
        body_out = dict(result)

    headers = {
        "X-Provider-Type": provider.provider_type,
        "X-Resolved-Provider": provider.name,
        "X-Resolved-Model": litellm_model,
        "X-Embed-Latency-Ms": f"{elapsed_ms:.1f}",
    }
    return JSONResponse(content=body_out, headers=headers)
