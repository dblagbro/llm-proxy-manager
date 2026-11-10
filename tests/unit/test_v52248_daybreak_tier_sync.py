"""v5.22.48 — Daybreak Blue authorization must round-trip via cluster sync.

Surfaced 2026-10-30 during the v5.22.47 Daybreak Blue rollout: after
authorizing a key for Daybreak Blue on www1 (``api_keys.daybreak_tier
= 'blue'``) and restoring www2, the authorization did **not** appear on
www2 — the ``daybreak`` model alias had synced but the key tier had not.

Root cause: the api_keys push payload (``manager.py``) and the apply
handler (``sync_handlers._apply_api_keys``) both omitted the new
``daybreak_tier`` column, so authorizing a key on one node never
reached peers. Same bug class as v4.4.18 (semantic_cache) and v5.0.0
(compliance policy fields): an operator-settable per-key column that
doesn't round-trip through sync.

v5.22.48 adds ``daybreak_tier`` to the push payload and to both the
update (membership-test) and INSERT (full-field) branches of the apply
handler, under the same discipline as the other policy columns.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture
async def fresh_db():
    from app.models.database import AsyncSessionLocal, engine
    from app.models.db import ApiKey, Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ApiKey))
        await cleanup.commit()
    yield AsyncSessionLocal
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ApiKey))
        await cleanup.commit()


# ── Push payload coverage ────────────────────────────────────────


def test_push_payload_includes_daybreak_tier():
    """Source-level guard: the api_keys push builder must emit
    ``daybreak_tier`` so authorizations propagate cluster-wide."""
    src = Path("app/cluster/manager.py").read_text()
    idx = src.index("keys = [")
    block = src[idx:idx + 3000]
    assert "daybreak_tier" in block, "push payload missing daybreak_tier"


# ── Apply handler — behavioral ───────────────────────────────────


@pytest.mark.asyncio
async def test_daybreak_tier_propagates_on_update(fresh_db):
    """A peer authorizing a key (daybreak_tier='blue') must update the
    local row — the exact www1→www2 case that regressed."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="key-db-1", name="cursor-ws2",
            key_hash="hash-db-1", key_prefix="llmp-db1",
            enabled=True, daybreak_tier=None,
        ))
        await db.commit()

        await apply_sync(db, {"api_keys": [{
            "id": "key-db-1", "name": "cursor-ws2",
            "key_hash": "hash-db-1", "key_prefix": "llmp-db1",
            "daybreak_tier": "blue",
            "last_user_edit_at": 1_900_000_000.0,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "hash-db-1")
        )).scalar_one()
        assert row.daybreak_tier == "blue", (
            "daybreak_tier='blue' from peer did not propagate"
        )


@pytest.mark.asyncio
async def test_daybreak_tier_set_on_insert(fresh_db):
    """A freshly-materialized key from a peer must carry its
    authorization tier (full-field INSERT, BUG-084 discipline)."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        await apply_sync(db, {"api_keys": [{
            "id": "key-db-2", "name": "new-authorized",
            "key_hash": "hash-db-2", "key_prefix": "llmp-db2",
            "enabled": True,
            "daybreak_tier": "blue",
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "hash-db-2")
        )).scalar_one()
        assert row.daybreak_tier == "blue"


@pytest.mark.asyncio
async def test_missing_daybreak_tier_doesnt_clobber(fresh_db):
    """A pre-v5.22.48 peer omits daybreak_tier entirely; it must NOT
    revoke a local authorization back to None."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="key-db-3", name="authorized-local",
            key_hash="hash-db-3", key_prefix="llmp-db3",
            enabled=True, daybreak_tier="blue",
        ))
        await db.commit()

        # Legacy peer payload: no daybreak_tier key at all
        await apply_sync(db, {"api_keys": [{
            "id": "key-db-3", "name": "authorized-local",
            "key_hash": "hash-db-3", "key_prefix": "llmp-db3",
            "spending_cap_usd": 10.0,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "hash-db-3")
        )).scalar_one()
        assert row.daybreak_tier == "blue", (
            "legacy peer omitting daybreak_tier clobbered local auth"
        )
        assert row.spending_cap_usd == 10.0
