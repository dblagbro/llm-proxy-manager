"""v4.4.25 — api_keys cluster-sync INSERT path full field coverage.

BUG-084, found 2026-05-28 during post-v4.4.24 verification of the
BUG-079 fix. The live LWW test propagated the row + stamp to peers
(BUG-079 fixed) but `semantic_cache_enabled` / `daily_hard_cap_usd`
arrived at their defaults — the operator's PATCHed values were lost.

Root cause: the apply_sync INSERT path (`app/cluster/sync.py`) only
materialized base columns + the stamp. The 8 extended operator-
settable fields (added to the UPDATE push/apply in v4.4.18) were
absent from the INSERT. And because the insert set
`last_user_edit_at` to the origin's stamp, the next sync hit the LWW
tie (equal stamps → keep local) so the UPDATE path never backfilled
them. A new key's extended fields therefore never reached peers
unless the operator PATCHed a second time.

v4.4.25 adds all 8 extended fields to the INSERT path.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


def test_insert_path_includes_extended_fields():
    """Source guard — the api_keys INSERT in apply_sync must carry
    every operator-settable field, matching the UPDATE-path coverage.
    v5.0.10 — extracted to sync_handlers._apply_api_keys."""
    src = Path("app/cluster/sync_handlers.py").read_text()
    # Find the db.add(ApiKey(...)) insert block
    idx = src.index("db.add(ApiKey(")
    block = src[idx:idx + 1600]
    for field in (
        "semantic_cache_enabled",
        "daily_soft_cap_usd",
        "daily_hard_cap_usd",
        "hourly_cap_usd",
        "rate_limit_tier",
        "caller_memory_ttl_days",
        "lmrh_polling_rpm",
        "lmrh_quotes_rpm",
        # v5.0.0 — compliance per-key policy fields. Same BUG-084 class
        # of bug if they're missing from the INSERT branch.
        "blocked_companies",
        "allowed_paths",
        "debug_echo_enabled",
    ):
        assert field in block, f"INSERT path missing {field} (BUG-084)"


@pytest_asyncio.fixture
async def fresh_db():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, ApiKey
    async with engine.begin() as conn:
        await conn.run_sync(ApiKey.__table__.drop, checkfirst=True)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ApiKey))
        await cleanup.commit()
    yield AsyncSessionLocal
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ApiKey))
        await cleanup.commit()


@pytest.mark.asyncio
async def test_new_key_materializes_extended_fields(fresh_db):
    """The exact BUG-084 repro: a peer sends a brand-new key (no local
    row) carrying operator-set extended fields + a stamp. The insert
    must persist those fields, not just the base columns."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        await apply_sync(db, {"api_keys": [{
            "id": "k-bug084", "name": "bug084",
            "key_hash": "h-bug084", "key_prefix": "llmp-84",
            "key_type": "standard", "enabled": True,
            "semantic_cache_enabled": True,
            "daily_hard_cap_usd": 3.33,
            "hourly_cap_usd": 1.10,
            "rate_limit_tier": "premium",
            "caller_memory_ttl_days": 30,
            "last_user_edit_at": 1779949351.14,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "h-bug084")
        )).scalar_one()
        # Every operator-set field must have survived the insert
        assert row.semantic_cache_enabled is True, "semantic_cache lost on insert (BUG-084)"
        assert row.daily_hard_cap_usd == 3.33, "daily_hard_cap lost on insert"
        assert row.hourly_cap_usd == 1.10, "hourly_cap lost on insert"
        assert row.rate_limit_tier == "premium", "rate_limit_tier lost on insert"
        assert row.caller_memory_ttl_days == 30, "caller_memory_ttl lost on insert"
        assert row.last_user_edit_at == 1779949351.14, "stamp lost on insert"


@pytest.mark.asyncio
async def test_insert_then_tie_update_is_consistent(fresh_db):
    """Regression for the interaction with the LWW tie: after the
    full-coverage insert, a second sync carrying the SAME stamp (a tie)
    must be a no-op that LEAVES the already-correct fields in place
    (not revert them). Proves the insert + LWW gate compose correctly."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    payload_key = {
        "id": "k-tie84", "name": "tie84",
        "key_hash": "h-tie84", "key_prefix": "llmp-t84",
        "key_type": "standard", "enabled": True,
        "semantic_cache_enabled": True,
        "daily_hard_cap_usd": 9.99,
        "last_user_edit_at": 2000.0,
    }
    async with fresh_db() as db:
        # First sync — insert
        await apply_sync(db, {"api_keys": [payload_key]})
        await db.commit()
        # Second sync — same stamp (tie). Must not revert.
        await apply_sync(db, {"api_keys": [payload_key]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "h-tie84")
        )).scalar_one()
        assert row.semantic_cache_enabled is True
        assert row.daily_hard_cap_usd == 9.99
