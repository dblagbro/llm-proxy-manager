"""grok-web dispatch + failover for /v1/chat/completions.

v5.22.37 — extracted from ``app/api/completions.py`` to get it under the
900-LOC pin in ``test_v5723_phase2_shared_helpers``. Unlike the sibling
claude-oauth branch, this one does **not** always return: when the grok.com
bridge fails, it re-resolves a provider and the handler falls through to the
litellm dispatch path with the new route.

So the contract is a two-tuple ``(response, route)``:

- ``(response, route)`` with a response — hand it straight back to the caller.
- ``(None, new_route)`` — the bridge failed over; rebind ``route`` to
  ``new_route`` and continue down the litellm path.

``extra`` and ``resp_headers`` are dicts and are mutated in place, exactly as
they were inline, so they need no return channel. That is why the signature
takes them rather than rebuilding them: the failover swaps ``extra`` to the new
provider's ``litellm_kwargs`` (v5.1.0 / Batch A4) and stamps two
``X-Grok-Web-Failover*`` headers.

The dispatch itself already lived in ``_grok_web_dispatch``; what moved here is
the *failover* that surrounds it, which belongs next to the call it recovers
from rather than inline in the handler.

Original rationale, preserved verbatim:

    # v3.2.0: grok-web on /v1/chat/completions. Operator's grok.com web
    # subscription. v3.2.9 extracted dispatch into a shared module; see
    # _grok_web_dispatch.dispatch_grok_web_openai.
    # v5.0.23 / Batch 2.5 — failover wiring (see messages.py for the
    # symmetric comment + decision rationale).
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

from app.routing.litellm_binding import OWN_DISPATCHER_PROVIDER_TYPES

logger = logging.getLogger(__name__)
router = APIRouter()


# pool-leak-audit: watchdog+bounded
# Same rationale as app/api/messages.py::messages — the disconnect
# watchdog releases the session on abort; LLM streams are bounded
# (~60s max) so the session isn't held for the multi-hour class of
# leak that v5.21.7 fixed in runs.py.
@router.post("/v1/chat/completions")


async def dispatch_grok_web_with_failover(
    *,
    route,
    body: dict,
    stream: bool,
    db,
    hint,
    key_record,
    extra: dict,
    resp_headers: dict,
    llm_hint,
):
    """Dispatch to grok-web, failing over to another provider if it errors.

    Returns ``(response, route)`` — see the module docstring. Raises
    ``HTTPException(502)`` when the bridge fails and no alternative provider
    can serve the requested model.
    """
    from app.api._grok_web_dispatch import dispatch_grok_web_openai
    gw_resp = await dispatch_grok_web_openai(
        route=route, body=body, stream=stream, resp_headers=resp_headers,
        db=db, key_record_id=key_record.id, t0=time.monotonic(),
        llm_hint=llm_hint,
    )
    if gw_resp is not None:
        # Tuple, not a bare response: the caller rebinds `route` from the
        # second element. `route` is unchanged on the success path.
        return gw_resp, route
    # Failover — re-resolve excluding the failed grok-web provider.
    from app.routing.router import select_provider
    failed_id = route.provider.id
    new_route = await select_provider(
        db=db, hint=hint,
        has_tools=False, has_images=False,
        key_type=key_record.key_type or "standard",
        model_override=body.get("model"),
        exclude_provider_id=failed_id,
        # v5.22.22 — this failover falls through to the litellm
        # dispatch path, so a provider with its own dispatcher
        # must not be a candidate. Same reasoning as v5.22.21.
        excluded_provider_types=OWN_DISPATCHER_PROVIDER_TYPES,
    )
    if new_route is None or new_route.provider.provider_type == "grok-web":
        raise HTTPException(
            502,
            "grok-web upstream failed and no alternative provider "
            "is available for the requested model.",
        )
    logger.info(
        "grok_web.failover_to provider=%s (was=%s, model=%s)",
        new_route.provider.name, failed_id, body.get("model"),
    )
    # v5.0.23 — preserve the caller's model intent through the
    # failover. select_provider sets cross_family_fallback +
    # rewrites litellm_model to OpenRouter's default
    # (openai/gpt-4o) when the OpenRouter provider's capability
    # scan doesn't list grok-3. For grok-web → openrouter we
    # want the original model (build_litellm_model maps
    # `grok-3` to `openrouter/x-ai/grok-3`). Clear the flag AND
    # rebuild litellm_model from the original model.
    # v5.22.22 — that reasoning holds for OpenRouter and ONLY OpenRouter.
    # For any other failover target, preserving the caller's model forces
    # a pairing the provider cannot serve (measured: `anthropic/grok-3` on
    # a claude-oauth provider, 41 times in 24h). Gate on whether the new
    # provider's type is actually in the model's family.
    from app.routing.router import failover_preserves_model

    if failover_preserves_model(new_route.provider, body.get("model")):
        new_route.cross_family_fallback = False
        new_route.served_model_native = None
        from app.routing.litellm_binding import build_litellm_model as _bld
        new_route.litellm_model = _bld(new_route.provider, body.get("model"))
    else:
        from app.routing.litellm_binding import build_litellm_model as _bld
        new_route.cross_family_fallback = True
        new_route.litellm_model = _bld(new_route.provider, None)
        logger.info(
            "grok_web.failover_model_substituted provider=%s type=%s "
            "requested=%s served=%s",
            new_route.provider.name, new_route.provider.provider_type,
            body.get("model"), new_route.litellm_model,
        )
    # v5.1.0 / Batch A4 — swap ``extra`` (litellm_kwargs) to the
    # new provider's. Pre-fix, the existing ``extra`` was built
    # from the grok-web provider's kwargs (no api_key for the
    # OpenRouter call); the litellm dispatch silently fell back
    # to whatever litellm could resolve, served openai/gpt-4o.
    # Mirrors the claude-oauth → litellm chain swap pattern
    # (messages.py:471).
    for _k in list(route.litellm_kwargs.keys()):
        extra.pop(_k, None)
    extra.update(new_route.litellm_kwargs)
    route = new_route
    resp_headers["X-Grok-Web-Failover"] = "true"
    resp_headers["X-Grok-Web-Failover-Target"] = new_route.provider.provider_type
    return None, new_route
