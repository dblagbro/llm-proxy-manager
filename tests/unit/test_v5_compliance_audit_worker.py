"""v5.0.1 — daily compliance audit worker (integrity hash + retention purge).

Pins:
- ``_run_one_sweep`` writes a ``compliance_audit_chain`` row for the
  closed prior UTC day with content derived from that day's events.
- The chain hash links forward (sha256 over prior_day_hash + sorted
  event content).
- A second sweep on the same day is idempotent (no new chain row,
  same hash).
- The retention purge drops rows older than ``retention_days`` and
  preserves newer ones.
- ``get_last_sweep`` returns a snapshot the admin endpoint can read.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.compliance.audit import (
    emit_event,
    generate_audit_id,
    purge_expired_events,
)
from app.models.db import ComplianceAuditChain, ComplianceEvent
from app.models.database import AsyncSessionLocal
from app.monitoring.compliance_audit_worker import (
    _run_one_sweep,
    get_last_sweep,
)


pytestmark = pytest.mark.asyncio


async def _seed_event(days_ago: int, audit_tag: str) -> int:
    async with AsyncSessionLocal() as db:
        e = await emit_event(
            db,
            audit_id=generate_audit_id(),
            api_key_id="testkey",
            event_type="model_substitution",
            reason_code=f"test-{audit_tag}",
            http_status=200,
            commit=True,
        )
        backdate = datetime.utcnow() - timedelta(days=days_ago)
        await db.execute(
            update(ComplianceEvent)
            .where(ComplianceEvent.id == e.id)
            .values(created_at=backdate, requested_at=backdate)
        )
        await db.commit()
        return e.id


async def _clear():
    async with AsyncSessionLocal() as db:
        await db.execute(ComplianceEvent.__table__.delete())
        await db.execute(ComplianceAuditChain.__table__.delete())
        await db.commit()


async def test_run_one_sweep_writes_prior_day_chain_row():
    await _clear()
    await _seed_event(1, "yesterday")

    await _run_one_sweep()

    async with AsyncSessionLocal() as db:
        rs = await db.execute(select(ComplianceAuditChain))
        rows = rs.scalars().all()
    assert len(rows) == 1
    assert rows[0].row_count == 1
    assert len(rows[0].chain_hash) == 64  # sha256 hex


async def test_run_one_sweep_is_idempotent_within_day():
    await _clear()
    await _seed_event(1, "yesterday")

    await _run_one_sweep()
    snap1 = get_last_sweep()
    await _run_one_sweep()
    snap2 = get_last_sweep()

    async with AsyncSessionLocal() as db:
        count = (
            await db.execute(select(func.count()).select_from(ComplianceAuditChain))
        ).scalar_one()
    assert count == 1
    # Hash is stable for the same day's content
    assert snap1["last_hash"] == snap2["last_hash"]


async def test_run_one_sweep_handles_empty_day():
    """Even with zero events for the prior day, a chain row is written
    (with row_count=0) so the chain is unbroken."""
    await _clear()

    await _run_one_sweep()

    async with AsyncSessionLocal() as db:
        rs = await db.execute(select(ComplianceAuditChain))
        rows = rs.scalars().all()
    assert len(rows) == 1
    assert rows[0].row_count == 0


async def test_purge_drops_rows_older_than_retention():
    await _clear()
    old_id = await _seed_event(30, "old")
    mid_id = await _seed_event(5, "mid")
    new_id = await _seed_event(1, "new")

    async with AsyncSessionLocal() as db:
        n = await purge_expired_events(db, retention_days=7)
    assert n == 1

    async with AsyncSessionLocal() as db:
        rs = await db.execute(select(ComplianceEvent.id).order_by(ComplianceEvent.id))
        ids = [r[0] for r in rs.all()]
    assert old_id not in ids
    assert mid_id in ids
    assert new_id in ids


async def test_purge_zero_retention_is_noop():
    """retention_days=0 is treated as "no policy" — purge does nothing."""
    await _clear()
    await _seed_event(30, "should-stay")
    async with AsyncSessionLocal() as db:
        n = await purge_expired_events(db, retention_days=0)
    assert n == 0


async def test_chain_links_forward():
    """The chain_hash of day N+1 depends on day N's chain_hash."""
    await _clear()
    # Seed events for two consecutive days
    await _seed_event(2, "two-days-ago")
    await _seed_event(1, "yesterday")

    # Manually compute both days' chains
    from app.compliance.audit import compute_daily_integrity_hash
    today = datetime.utcnow().date()
    day_m2 = today - timedelta(days=2)
    day_m1 = today - timedelta(days=1)

    async with AsyncSessionLocal() as db:
        h_m2 = await compute_daily_integrity_hash(db, day_m2)
    async with AsyncSessionLocal() as db:
        h_m1 = await compute_daily_integrity_hash(db, day_m1)

    async with AsyncSessionLocal() as db:
        rs = await db.execute(
            select(ComplianceAuditChain).where(
                ComplianceAuditChain.day == day_m1.isoformat()
            )
        )
        row_m1 = rs.scalar_one()
    assert row_m1.prior_day_chain_hash == h_m2
    assert h_m1 != h_m2
