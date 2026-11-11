"""v5.22.50 — record_request must not crash on duplicate metric buckets.

`provider_metrics` has no UNIQUE constraint on (provider_id, bucket_ts) and
`record_request` is a non-atomic SELECT-then-INSERT, so two concurrent writers
on a cold bucket can both INSERT. The subsequent read used
`scalar_one_or_none()`, which raised `sqlalchemy.exc.MultipleResultsFound` the
next time that bucket was touched — propagating out of `record_outcome` and
turning live requests into 502s (mis-attributed to whichever provider the
except-handler re-recorded). v5.22.50 folds onto the oldest row with
`order_by(id).scalars().first()` so a duplicate bucket can never crash the
request path.

Surfaced 2026-11-01: OpenRouter/Cohere/Grok/OpenAI providers all had duplicate
(provider_id, bucket_ts) rows on the live node; cross-family requests 502'd.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture
async def fixture_db():
    from app.models.database import AsyncSessionLocal, engine
    from app.models.db import Base, Provider

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(Provider).where(Provider.id == "dupmetric-prov"))
        await cleanup.commit()
    async with AsyncSessionLocal() as db:
        db.add(Provider(
            id="dupmetric-prov", name="dupmetric-test",
            provider_type="openrouter", priority=10, enabled=True,
        ))
        await db.commit()

    yield AsyncSessionLocal

    async with AsyncSessionLocal() as cleanup:
        from app.models.db_provider import ProviderMetric
        await cleanup.execute(
            delete(ProviderMetric).where(ProviderMetric.provider_id == "dupmetric-prov")
        )
        await cleanup.execute(delete(Provider).where(Provider.id == "dupmetric-prov"))
        await cleanup.commit()


@pytest.mark.asyncio
async def test_record_request_tolerates_duplicate_bucket(fixture_db):
    """Two rows sharing (provider_id, bucket_ts) must not raise; the oldest
    row is incremented."""
    from app.models.database import AsyncSessionLocal
    from app.models.db_provider import ProviderMetric
    from app.monitoring.metrics import _bucket, record_request

    bucket = _bucket()
    # Seed the exact duplicate condition record_request will read back.
    async with AsyncSessionLocal() as db:
        for _ in range(2):
            db.add(ProviderMetric(
                provider_id="dupmetric-prov", bucket_ts=bucket,
                requests=1, successes=1, failures=0,
                total_tokens=10, total_cost_usd=0.0, avg_latency_ms=5.0,
                circuit_state="closed",
            ))
        await db.commit()

    # Pre-fix this raised MultipleResultsFound.
    async with AsyncSessionLocal() as db:
        await record_request(
            db=db, provider_id="dupmetric-prov", success=True,
            input_tokens=3, output_tokens=4, latency_ms=12.0, cost_usd=0.0,
        )
        await db.commit()

    # Exactly one of the two rows got the increment (the oldest); no crash.
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ProviderMetric)
            .where(ProviderMetric.provider_id == "dupmetric-prov",
                   ProviderMetric.bucket_ts == bucket)
            .order_by(ProviderMetric.id)
        )).scalars().all()
    assert len(rows) == 2, "read-side fix must not delete the straggler"
    assert rows[0].requests == 2, "oldest row should have been incremented"
    assert rows[1].requests == 1, "straggler row untouched"
