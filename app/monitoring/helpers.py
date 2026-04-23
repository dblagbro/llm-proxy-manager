"""Shared post-request outcome recorder.

Centralises the record_success/record_failure + estimate_cost + record_request
pattern that appears in every streaming and non-streaming handler.
"""
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.routing.circuit_breaker import record_success, record_failure, is_billing_error
from app.monitoring.metrics import record_request
from app.monitoring.pricing import estimate_cost
from app.monitoring.activity import log_event
from app.observability.prometheus import observe_request, observe_ttft, observe_cache_tokens
from app.routing.hedging import record_ttft_sample
from app.budget.tracker import record_cost


async def record_outcome(
    db: AsyncSession,
    provider_id: str,
    model: str,
    *,
    success: bool,
    in_tok: int = 0,
    out_tok: int = 0,
    t0: float = 0.0,
    key_record_id: str,
    error_str: str = "",
    ttft_ms: float = 0.0,
    endpoint: str = "messages",
    cache_creation: int = 0,
    cache_read: int = 0,
) -> None:
    if success:
        latency_ms = (time.monotonic() - t0) * 1000
        cost = estimate_cost(model, in_tok, out_tok)
        await record_success(provider_id)
        await record_request(db, provider_id, True, in_tok, out_tok, latency_ms, cost, key_record_id, ttft_ms)
        await record_cost(db, key_record_id, cost)
        observe_request(
            provider=provider_id, model=model, endpoint=endpoint,
            success=True, duration_sec=latency_ms / 1000.0,
            in_tokens=in_tok, out_tokens=out_tok, cost_usd=cost,
        )
        if ttft_ms > 0:
            observe_ttft(provider_id, model, ttft_ms / 1000.0)
            record_ttft_sample(provider_id, ttft_ms)
        if cache_creation or cache_read:
            observe_cache_tokens(provider_id, model, cache_creation, cache_read)
        await log_event(
            db,
            event_type="llm_request",
            message=f"{model}",
            severity="info",
            provider_id=provider_id,
            api_key_id=key_record_id,
            metadata={
                "model": model,
                "in_tok": in_tok,
                "out_tok": out_tok,
                "cost_usd": round(cost, 6),
                "latency_ms": round(latency_ms, 1),
            },
        )
    else:
        await record_failure(provider_id, billing_error=is_billing_error(error_str))
        await record_request(db, provider_id, False, 0, 0, 0, 0, key_record_id)
        observe_request(
            provider=provider_id, model=model, endpoint=endpoint,
            success=False, duration_sec=0.0,
            in_tokens=0, out_tokens=0, cost_usd=0.0,
        )
        await log_event(
            db,
            event_type="llm_request",
            message=f"{model} — error",
            severity="warning",
            provider_id=provider_id,
            api_key_id=key_record_id,
            metadata={"model": model, "error": error_str[:200] if error_str else None},
        )
