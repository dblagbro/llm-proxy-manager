"""
M6 — upstream 429 retry with exponential backoff + jitter.

Wraps litellm.acompletion: on RateLimitError, reads Retry-After header,
waits (capped at 30 s), and retries up to max_retries times.
Billing errors are never retried — caller's record_outcome handles CB trip.

v3.0.14: also catches NotFoundError when the failing model is in our
deprecation registry, persists the replacement on providers.default_model,
and retries once with the new model on the same upstream.
"""
import asyncio
import logging
import random
from typing import Optional

import litellm
from litellm import RateLimitError

# v3.0.14: NotFoundError isn't always present on the litellm stub used by
# unit tests; resolve it defensively so importing retry.py doesn't blow up
# in test envs that pre-stub litellm with only RateLimitError.
NotFoundError = getattr(litellm, "NotFoundError", None) or type(
    "NotFoundError", (Exception,), {}
)

from app.routing.circuit_breaker import is_billing_error
from app.providers.deprecations import MODEL_DEPRECATIONS

logger = logging.getLogger(__name__)

_MAX_WAIT_SEC = 30.0


def _strip_prefix(model: str) -> str:
    """Strip litellm-style ``provider/`` prefix if present."""
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def _replacement_for(model: str) -> Optional[str]:
    """Return the replacement model id for ``model`` if it (or its
    prefix-stripped form) is in MODEL_DEPRECATIONS.

    Preserves the original litellm prefix on the result — e.g.
    ``gemini/gemini-2.0-flash`` -> ``gemini/gemini-2.5-flash``.
    """
    direct = MODEL_DEPRECATIONS.get(model)
    if direct:
        return direct
    bare = _strip_prefix(model)
    bare_repl = MODEL_DEPRECATIONS.get(bare)
    if not bare_repl:
        return None
    if "/" in model:
        prefix = model.split("/", 1)[0]
        if "/" in bare_repl:
            # Replacement already has its own prefix — trust the registry.
            return bare_repl
        return f"{prefix}/{bare_repl}"
    return bare_repl


async def _persist_default_model_bump(old_model: str, new_model: str) -> int:
    """Update every active provider whose ``default_model`` equals
    ``old_model`` (prefix-stripped) to ``new_model`` (prefix-stripped).
    Returns the number of rows updated.
    """
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider
    from sqlalchemy import select

    bare_old = _strip_prefix(old_model)
    bare_new = _strip_prefix(new_model)
    updated = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Provider).where(
                Provider.default_model == bare_old,
                Provider.deleted_at.is_(None),
            )
        )
        for p in result.scalars().all():
            p.default_model = bare_new
            updated += 1
        if updated:
            await db.commit()
    return updated


async def acompletion_with_retry(
    model: str,
    messages: list,
    max_retries: int = 3,
    **kwargs,
):
    deprecation_retry_done = False
    current_model = model
    for attempt in range(max_retries + 1):
        try:
            return await litellm.acompletion(model=current_model, messages=messages, **kwargs)
        except NotFoundError as exc:
            # v3.0.14: catch model-deprecation 404s and auto-bump if the
            # registry knows the replacement. One retry per call — if the
            # new model also fails, fall through to the caller's CB / next-
            # provider flow.
            if deprecation_retry_done:
                raise
            replacement = _replacement_for(current_model)
            if not replacement:
                raise
            try:
                bumped = await _persist_default_model_bump(current_model, replacement)
            except Exception as bump_exc:
                logger.warning(
                    "deprecation.runtime_bump_persist_failed model=%s err=%s",
                    current_model, bump_exc,
                )
                bumped = 0
            logger.warning(
                "deprecation.runtime_bump model=%s -> %s persisted_rows=%d cause=%s",
                current_model, replacement, bumped, str(exc)[:200],
            )
            current_model = replacement
            deprecation_retry_done = True
            # don't sleep — the upstream is responsive, only the model id was stale
            continue
        except RateLimitError as exc:
            if is_billing_error(str(exc)):
                raise
            if attempt >= max_retries:
                raise
            retry_after = _parse_retry_after(exc)
            wait = _backoff(attempt, retry_after)
            logger.warning(
                "upstream_rate_limit",
                extra={"model": current_model, "attempt": attempt + 1, "wait_sec": round(wait, 1)},
            )
            await asyncio.sleep(wait)


def _parse_retry_after(exc: RateLimitError) -> float:
    response = getattr(exc, "response", None)
    if response is not None:
        ra = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if ra:
            try:
                return max(0.0, float(ra))
            except ValueError:
                pass
    return 5.0  # default when header is absent


def _backoff(attempt: int, retry_after: float) -> float:
    exp = min(retry_after, (2 ** attempt) * 2.0)
    jitter = random.uniform(0, exp * 0.25)
    return min(exp + jitter, _MAX_WAIT_SEC)
