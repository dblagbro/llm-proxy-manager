"""BUG-079 regression — apply_sync must survive duplicate
(provider_id, captured_at) rows in provider_ai_review.

Pre-v4.4.24 ``_apply_provider_ai_reviews`` used ``scalar_one_or_none()`` with
no ``.limit(1)``; a legacy duplicate raised MultipleResultsFound and rolled
back the WHOLE inbound sync. v4.4.27 added a UNIQUE INDEX in ``init_db`` so new
dupes cannot be written, but nodes mid-rolling-deploy (or legacy DBs) can still
carry them, so the ``.limit(1)`` guard must keep working end to end.

The test DB is built with ``create_all`` (no ux_ index), so a duplicate can be
inserted here; any index that happens to exist is dropped first.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text


@pytest_asyncio.fixture
async def fresh_db():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, Provider, ProviderAiReview

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(
            "DROP INDEX IF EXISTS ux_provider_ai_review_provider_id_captured_at"))
    async with AsyncSessionLocal() as c:
        await c.execute(delete(ProviderAiReview))
        await c.execute(delete(Provider))
        await c.commit()
    yield AsyncSessionLocal
    async with AsyncSessionLocal() as c:
        await c.execute(delete(ProviderAiReview))
        await c.execute(delete(Provider))
        await c.commit()


@pytest.mark.asyncio
async def test_apply_sync_survives_duplicate_provider_ai_review(fresh_db):
    from app.cluster.sync import apply_sync
    from app.models.db import Provider, ProviderAiReview

    captured = datetime(2026, 5, 21, 12, 8, 9)
    async with fresh_db() as db:
        db.add(Provider(
            id="pdup", name="dup-prov", provider_type="claude-oauth",
            priority=10, enabled=True, extra_config={},
            updated_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        # Two rows with the same (provider_id, captured_at): the stale one
        # (no lifecycle) and the one the operator acted on.
        db.add(ProviderAiReview(
            provider_id="pdup", captured_at=captured, llm_verdict="watch"))
        db.add(ProviderAiReview(
            provider_id="pdup", captured_at=captured, llm_verdict="watch",
            applied_at=datetime(2026, 5, 21, 13, 0, 0)))
        await db.commit()

        payload = {"provider_ai_reviews": [{
            "provider_id": "pdup",
            "captured_at": captured.replace(tzinfo=timezone.utc).isoformat(),
            "llm_verdict": "watch",
            "dismissed_at": "2026-05-21T14:00:00+00:00",
        }]}
        # Must not raise MultipleResultsFound.
        await apply_sync(db, payload)
        await db.commit()

        n = (await db.execute(
            select(func.count()).select_from(ProviderAiReview)
            .where(ProviderAiReview.provider_id == "pdup")
        )).scalar_one()
        assert n == 2, "apply_sync must neither crash nor add a third row"
