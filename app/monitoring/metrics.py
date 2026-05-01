"""
Time-series metrics collection.
Buckets provider usage into 5-minute windows for history graphs.
Also tracks per-API-key cumulative stats.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func

from app.models.db import ProviderMetric, ApiKey
from app.routing.circuit_breaker import get_all_states
from app.utils.timefmt import utc_iso

logger = logging.getLogger(__name__)

BUCKET_MINUTES = 5


def _bucket(dt: Optional[datetime] = None) -> datetime:
    dt = dt or datetime.utcnow()
    return dt.replace(second=0, microsecond=0, minute=(dt.minute // BUCKET_MINUTES) * BUCKET_MINUTES)


async def record_request(
    db: AsyncSession,
    provider_id: str,
    success: bool,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
    cost_usd: float,
    api_key_id: Optional[str] = None,
    ttft_ms: float = 0.0,
):
    bucket = _bucket()
    cb_states = get_all_states()
    circuit_state = cb_states.get(provider_id, {}).get("state", "closed")

    # Upsert into provider_metrics bucket
    result = await db.execute(
        select(ProviderMetric).where(
            ProviderMetric.provider_id == provider_id,
            ProviderMetric.bucket_ts == bucket,
        )
    )
    metric = result.scalar_one_or_none()

    if metric:
        metric.requests += 1
        metric.successes += (1 if success else 0)
        metric.failures += (0 if success else 1)
        metric.total_tokens += input_tokens + output_tokens
        metric.total_cost_usd += cost_usd
        # Rolling average latency
        if metric.requests > 0:
            metric.avg_latency_ms = (
                (metric.avg_latency_ms * (metric.requests - 1) + latency_ms) / metric.requests
            )
        if ttft_ms > 0 and success:
            metric.ttft_requests = (metric.ttft_requests or 0) + 1
            n = metric.ttft_requests
            metric.avg_ttft_ms = ((metric.avg_ttft_ms or 0) * (n - 1) + ttft_ms) / n
        metric.circuit_state = circuit_state
    else:
        metric = ProviderMetric(
            provider_id=provider_id,
            bucket_ts=bucket,
            requests=1,
            successes=1 if success else 0,
            failures=0 if success else 1,
            total_tokens=input_tokens + output_tokens,
            total_cost_usd=cost_usd,
            avg_latency_ms=latency_ms,
            avg_ttft_ms=ttft_ms if ttft_ms > 0 else 0.0,
            ttft_requests=1 if ttft_ms > 0 and success else 0,
            circuit_state=circuit_state,
        )
        db.add(metric)

    # Update API key cumulative stats
    if api_key_id and success:
        await db.execute(
            update(ApiKey)
            .where(ApiKey.id == api_key_id)
            .values(
                total_tokens=ApiKey.total_tokens + input_tokens + output_tokens,
                total_cost_usd=ApiKey.total_cost_usd + cost_usd,
            )
        )

    await db.commit()


async def get_provider_history(
    db: AsyncSession,
    provider_id: str,
    hours: int = 24,
) -> list[dict]:
    since = datetime.utcnow() - timedelta(hours=hours)
    result = await db.execute(
        select(ProviderMetric)
        .where(
            ProviderMetric.provider_id == provider_id,
            ProviderMetric.bucket_ts >= since,
        )
        .order_by(ProviderMetric.bucket_ts)
    )
    rows = result.scalars().all()
    return [
        {
            "ts": utc_iso(r.bucket_ts),
            "requests": r.requests,
            "successes": r.successes,
            "failures": r.failures,
            "total_tokens": r.total_tokens,
            "total_cost_usd": r.total_cost_usd,
            "avg_latency_ms": r.avg_latency_ms,
            "avg_ttft_ms": r.avg_ttft_ms,
            "circuit_state": r.circuit_state,
        }
        for r in rows
    ]


async def get_all_provider_summary(db: AsyncSession, hours: int = 24) -> list[dict]:
    """Aggregate per-provider stats over the last N hours and join the live
    provider name. v2.9.0 fixes a missing-column AttributeError that crashed
    the endpoint (avg_ttft_ms was referenced but not selected) and returns
    the human-readable name so the UI doesn't show bare provider IDs."""
    from app.models.db import Provider
    since = datetime.utcnow() - timedelta(hours=hours)
    result = await db.execute(
        select(
            ProviderMetric.provider_id,
            func.sum(ProviderMetric.requests).label("requests"),
            func.sum(ProviderMetric.successes).label("successes"),
            func.sum(ProviderMetric.failures).label("failures"),
            func.sum(ProviderMetric.total_tokens).label("total_tokens"),
            func.sum(ProviderMetric.total_cost_usd).label("total_cost_usd"),
            func.avg(ProviderMetric.avg_latency_ms).label("avg_latency_ms"),
            func.avg(ProviderMetric.avg_ttft_ms).label("avg_ttft_ms"),
        )
        .where(ProviderMetric.bucket_ts >= since)
        .group_by(ProviderMetric.provider_id)
    )
    rows = result.all()
    cb_states = get_all_states()
    name_result = await db.execute(select(Provider.id, Provider.name))
    name_map = {pid: pname for pid, pname in name_result.all()}
    return [
        {
            "provider_id": r.provider_id,
            "provider_name": name_map.get(r.provider_id, r.provider_id),
            "requests": r.requests or 0,
            "successes": r.successes or 0,
            "failures": r.failures or 0,
            "success_rate": round((r.successes or 0) / max(r.requests or 1, 1) * 100, 1),
            "total_tokens": r.total_tokens or 0,
            "total_cost_usd": r.total_cost_usd or 0.0,
            "avg_latency_ms": round(r.avg_latency_ms or 0, 1),
            "avg_ttft_ms": round(r.avg_ttft_ms or 0, 1),
            "circuit_state": cb_states.get(r.provider_id, {}).get("state", "closed"),
        }
        for r in rows
    ]


async def prune_old_metrics(db: AsyncSession, keep_days: int = 90):
    cutoff = datetime.utcnow() - timedelta(days=keep_days)
    result = await db.execute(
        select(ProviderMetric).where(ProviderMetric.bucket_ts < cutoff)
    )
    old = result.scalars().all()
    for row in old:
        await db.delete(row)
    await db.commit()
    return len(old)
