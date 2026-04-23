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
) -> None:
    if success:
        latency_ms = (time.monotonic() - t0) * 1000
        cost = estimate_cost(model, in_tok, out_tok)
        await record_success(provider_id)
        await record_request(db, provider_id, True, in_tok, out_tok, latency_ms, cost, key_record_id, ttft_ms)
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
        await log_event(
            db,
            event_type="llm_request",
            message=f"{model} — error",
            severity="warning",
            provider_id=provider_id,
            api_key_id=key_record_id,
            metadata={"model": model, "error": error_str[:200] if error_str else None},
        )
