"""
v5.9.0 — OpenAI-compatible /v1/images/generations dispatch.

OpenAI shape:
    {model, prompt, n?, size?, quality?, response_format?, style?, user?}
    → {created, data:[{b64_json | url}, ...]}

No local-CPU fallback for diffusion models — image-gen is upstream-only.
Subscription-OAuth providers are excluded (no image-gen surface).

DevinGPT memo 2026-06-21 — flips ``services/image_gen.py`` to a single
proxy client, deletes the direct OPENAI_IMG_KEY path.
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
from app.routing.router import select_provider, build_litellm_kwargs, build_litellm_model
from app.utils.disconnect_watchdog import watch_for_disconnect

logger = logging.getLogger(__name__)
router = APIRouter(tags=["images"])


_AUTH = resolve_api_key_dep()


_EXCLUDED_TYPES = {"claude-oauth", "ChatGPT-oauth-plan", "grok-web", "cursor-oauth"}


@router.post("/v1/images/generations")
async def images_generations(
    request: Request,
    # v5.21.14 — db BEFORE _watchdog (LIFO cleanup closes get_db last). See cluster.py v5.21.12.
    db: AsyncSession = Depends(get_db),
    _watchdog: None = Depends(watch_for_disconnect),  # v5.9.9 — see messages.py
    key_record=Depends(_AUTH),
):
    body = await request.json()
    model = body.get("model")
    prompt = body.get("prompt")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(400, "Missing required field: model")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(400, "Missing required field: prompt")

    try:
        route = await select_provider(
            db, hint=None, has_tools=False, has_images=True,
            key_type=key_record.key_type,
            pinned_provider_id=None, model_override=model,
            sort_mode=None,
            excluded_provider_types=_EXCLUDED_TYPES,
        )
    except RuntimeError as e:
        raise HTTPException(
            503,
            f"No image-gen provider available for model {model!r}: {e}. "
            "Add a provider whose scanned capabilities include this model, "
            "or pick a different model.",
        )

    provider = route.provider
    kwargs = build_litellm_kwargs(provider)
    litellm_model = build_litellm_model(provider, model_override=model)

    extra: dict[str, Any] = {}
    for k in ("n", "size", "quality", "response_format", "style", "user", "background"):
        if k in body and body[k] is not None:
            extra[k] = body[k]

    t0 = time.monotonic()
    try:
        result = await litellm.aimage_generation(
            model=litellm_model, prompt=prompt, **kwargs, **extra,
        )
    except Exception as e:
        from app.routing.circuit_breaker import record_failure, is_billing_error
        err_str = str(e)
        billing = is_billing_error(err_str)
        await record_failure(provider.id, billing_error=billing)
        short = err_str.split("\nTraceback", 1)[0].strip()[:500]
        raise HTTPException(502, f"Image-gen upstream error: {short}")

    from app.routing.circuit_breaker import record_success
    await record_success(provider.id)
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    if hasattr(result, "model_dump"):
        body_out = result.model_dump()
    elif hasattr(result, "dict"):
        body_out = result.dict()
    else:
        body_out = dict(result) if not isinstance(result, dict) else result

    headers = {
        "X-Provider-Type": provider.provider_type,
        "X-Resolved-Provider": provider.name,
        "X-Resolved-Model": litellm_model,
        "X-ImageGen-Latency-Ms": f"{elapsed_ms:.1f}",
    }
    # v5.14.1 — response-shaping hook runner. Same contract as messages.py.
    try:
        from app.api._response_hook_runner import apply_response_hooks, HookContext
        await apply_response_hooks(
            handler_id="images",
            resp_headers=headers,
            context=HookContext(
                requested_model=body.get("model") if isinstance(body, dict) else None,
                served_model=litellm_model,
                api_key_id=getattr(key_record, "id", None),
                provider_id=getattr(provider, "id", None),
                key_record=key_record,
                request=request,
            ),
        )
    except Exception:
        pass
    return JSONResponse(content=body_out, headers=headers)
