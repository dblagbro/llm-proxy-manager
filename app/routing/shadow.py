"""Shadow traffic (Wave 3 #16).

Mirror a fraction of primary requests to a candidate provider async,
compute embedding-similarity between primary response and candidate,
emit a Prometheus histogram and an activity-log event. Zero user
impact — candidate response is discarded.

Settings:
  shadow_traffic_rate           float 0.0-1.0   fraction of requests to shadow
  shadow_candidate_provider_id  string          provider ID to shadow-test

Because this is pure observability work, failures are logged and
swallowed — a shadow error never impacts the primary response.
"""
from __future__ import annotations

import logging
import random

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def should_shadow(rate: float) -> bool:
    """Sample the rate; avoid random() when rate is 0 or 1."""
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    return random.random() < rate


async def run_shadow_compare(
    db_factory,
    candidate_provider_id: str,
    messages: list,
    primary_text: str,
    primary_model_id: str,
    request_kwargs: dict,
    embedding_model: str,
    embedding_dims: int,
    api_key_id: str,
) -> None:
    """Fire the candidate call, embed both answers, emit histogram + activity event.

    Runs as a BackgroundTask — no return value surfaced to the user.
    """
    try:
        # Load candidate provider in its own session to avoid coupling with
        # the primary request's DB transaction.
        from sqlalchemy import select
        from app.models.db import Provider
        from app.routing.router import build_litellm_model, build_litellm_kwargs
        from app.routing.retry import acompletion_with_retry

        async with db_factory() as db:
            p = (await db.execute(select(Provider).where(Provider.id == candidate_provider_id))).scalar_one_or_none()

        if not p or not p.enabled:
            return

        cand_model = build_litellm_model(p)
        cand_kwargs = build_litellm_kwargs(p)

        # Use the same request shape but strip streaming / anthropic-specific opts
        call_kwargs = {**cand_kwargs}
        if "max_tokens" in request_kwargs:
            call_kwargs["max_tokens"] = request_kwargs["max_tokens"]
        if "temperature" in request_kwargs:
            call_kwargs["temperature"] = request_kwargs["temperature"]
        if "system" in request_kwargs:
            call_kwargs["system"] = request_kwargs["system"]

        resp = await acompletion_with_retry(
            model=cand_model, messages=messages, stream=False, **call_kwargs,
        )
        cand_text = resp.choices[0].message.content or ""

        # Embed both; compare cosine
        similarity = await _embed_cosine(primary_text, cand_text, embedding_model, embedding_dims)
        if similarity is None:
            return

        # Prometheus histogram
        try:
            from app.observability.prometheus import observe_shadow_similarity
            observe_shadow_similarity(
                primary_model_id, cand_model, similarity,
            )
        except Exception:
            pass

        # Activity event
        try:
            from app.monitoring.activity import log_event
            async with db_factory() as db2:
                await log_event(
                    db2,
                    event_type="shadow_compare",
                    message=f"shadow {primary_model_id} vs {cand_model}",
                    severity="info",
                    api_key_id=api_key_id,
                    metadata={
                        "primary_model": primary_model_id,
                        "shadow_model": cand_model,
                        "similarity": round(similarity, 4),
                    },
                )
        except Exception as exc:
            logger.debug("shadow.log_event_failed %s", exc)

    except Exception as exc:
        logger.warning("shadow.compare_failed %s", exc)


async def _embed_cosine(
    text_a: str, text_b: str, model: str, dims: int,
) -> float | None:
    if not text_a or not text_b:
        return None
    try:
        import litellm
        import math
        resp = await litellm.aembedding(model=model, input=[text_a, text_b], dimensions=dims)
        data = resp.data if isinstance(resp.data, list) else resp.data
        vecs = [list(d.embedding) if hasattr(d, "embedding") else list(d["embedding"]) for d in data]
        if len(vecs) != 2:
            return None
        a, b = vecs
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return None
        return dot / (na * nb)
    except Exception as exc:
        logger.debug("shadow.embed_failed %s", exc)
        return None
