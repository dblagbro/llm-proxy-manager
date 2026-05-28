"""v4.4.24 — cluster-sync apply robustness.

Closes BUG-079 / BUG-080 / BUG-081 from the 2026-05-27 deep QA pass.

BUG-079: a single duplicate (provider_id, captured_at) row in
provider_ai_review made `_apply_provider_ai_reviews` raise
MultipleResultsFound, aborting the entire apply_sync transaction.
Cluster sync was silently broken for ~6 days; heartbeat still
reported healthy.

BUG-080: 5 of 7 apply handlers shared the same `.scalar_one_or_none()`
-without-`.limit(1)` vulnerability.

BUG-081: `push_sync` fire-and-forgot the POST — a peer 500-ing on
apply was invisible to the originator, which is why BUG-079 went
undetected.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


# ── Source guards: every handler must now have .limit(1) ─────────────


def test_all_apply_handlers_have_limit_guard():
    """Every scalar_one_or_none lookup in sync_handlers.py must be
    preceded by a .limit(1) so a duplicate row can't raise
    MultipleResultsFound and abort apply_sync."""
    src = Path("app/cluster/sync_handlers.py").read_text()
    # Find each scalar_one_or_none and confirm .limit(1) appears in the
    # preceding ~8 lines (the select chain).
    lines = src.splitlines()
    offenders = []
    for i, line in enumerate(lines):
        if "scalar_one_or_none()" in line:
            window = "\n".join(lines[max(0, i - 10):i + 1])
            if ".limit(1)" not in window:
                offenders.append(i + 1)
    assert not offenders, (
        f"scalar_one_or_none without .limit(1) guard at lines {offenders} "
        f"— BUG-080 regression risk (a duplicate row would crash apply_sync)"
    )


def test_push_sync_inspects_response_status():
    """push_sync must check the peer response status, not fire-and-forget."""
    src = Path("app/cluster/manager.py").read_text()
    idx = src.index("async def push_sync")
    block = src[idx:idx + 1800]
    # Must assign the response and check status_code
    assert "resp = await client.post" in block or "= await client.post" in block, (
        "push_sync must capture the POST response (BUG-081)"
    )
    assert "status_code != 200" in block, (
        "push_sync must check for non-200 peer responses (BUG-081)"
    )
    assert "logger.warning" in block, (
        "push_sync must log a warning on peer rejection (BUG-081)"
    )


def test_hours_query_has_lower_bound():
    """Query(hours) must reject negatives (BUG-083)."""
    src = Path("app/api/monitoring.py").read_text()
    # No occurrence of the old unbounded-low pattern should remain
    assert "Query(24, le=720)" not in src, (
        "found Query(24, le=720) without ge= lower bound — BUG-083"
    )
    assert "Query(24, ge=1, le=720)" in src, (
        "expected the ge=1 lower bound (BUG-083 fix)"
    )


# ── Behavioral: apply_sync survives duplicate provider_ai_review ─────


@pytest_asyncio.fixture
async def fresh_db():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, ProviderAiReview
    async with engine.begin() as conn:
        await conn.run_sync(ProviderAiReview.__table__.drop, checkfirst=True)
        await conn.run_sync(Base.metadata.create_all)
    yield AsyncSessionLocal
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ProviderAiReview))
        await cleanup.commit()


@pytest.mark.asyncio
async def test_apply_provider_ai_reviews_survives_duplicate(fresh_db):
    """The exact BUG-079 repro: seed a duplicate (provider_id,
    captured_at) row, then apply a sync payload referencing it. Pre-fix
    this raised MultipleResultsFound; post-fix it must succeed."""
    from datetime import datetime
    from app.cluster.sync_handlers import _apply_provider_ai_reviews
    from app.models.db import ProviderAiReview

    cap = datetime(2026, 5, 21, 12, 8, 9)
    async with fresh_db() as db:
        # Seed TWO rows with the same (provider_id, captured_at)
        for _ in range(2):
            db.add(ProviderAiReview(
                provider_id="dup-prov", captured_at=cap,
                llm_verdict="watch",
            ))
        await db.commit()

        # Apply a sync payload referencing the duplicate pair — must NOT raise
        await _apply_provider_ai_reviews(db, [{
            "provider_id": "dup-prov",
            "captured_at": cap.isoformat(),
            "llm_verdict": "promote",
            "applied_at": "2026-05-22T00:00:00",
        }])
        await db.commit()

        # The lifecycle field should have been applied to one of the rows
        rows = (await db.execute(
            select(ProviderAiReview).where(ProviderAiReview.provider_id == "dup-prov")
        )).scalars().all()
        assert len(rows) == 2, "apply must not delete/duplicate further"


@pytest.mark.asyncio
async def test_apply_caller_memory_survives_duplicate(fresh_db):
    """BUG-080: caller_memory handler must also tolerate a duplicate."""
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, CallerMemory
    async with engine.begin() as conn:
        await conn.run_sync(CallerMemory.__table__.drop, checkfirst=True)
        await conn.run_sync(Base.metadata.create_all)

    from app.cluster.sync_handlers import _apply_caller_memory
    async with AsyncSessionLocal() as db:
        for _ in range(2):
            db.add(CallerMemory(
                api_key_id="k1", conversation_id="c1",
                memory_tag="default", content="x", updated_at=1.0,
            ))
        await db.commit()

        # Must not raise MultipleResultsFound
        await _apply_caller_memory(db, [{
            "api_key_id": "k1", "conversation_id": "c1",
            "memory_tag": "default", "content": "y", "updated_at": 2.0,
        }])
        await db.commit()
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(CallerMemory))
        await cleanup.commit()
