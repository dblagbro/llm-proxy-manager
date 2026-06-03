"""v4.4.18 — api_keys cluster-sync field coverage.

Surfaced 2026-05-22 by F3 from the routing-cost research: the
operator-forwarded hub-team request to "flip semantic_cache on the
coordinator-hub key" succeeded on www1 but **did not propagate** to
peers via cluster sync. Investigation found:

- The push payload (`manager.py`) sent only 9 of the 26 api_keys
  columns.
- The apply handler (`sync.py`) wrote only 2 fields on update
  (`spending_cap_usd`, `rate_limit_rpm`).

This is the same class of bug as v3.0.10's provider-field coverage
fix — operator edits a field on one node, peers stay stale.

v4.4.18 expands both push + apply to cover the operator-settable
fields: ``enabled``, ``semantic_cache_enabled``, ``daily_soft_cap_usd``,
``daily_hard_cap_usd``, ``hourly_cap_usd``, ``rate_limit_tier``,
``caller_memory_ttl_days``, ``lmrh_polling_rpm``, ``lmrh_quotes_rpm``.

Open follow-up (not in scope here): api_keys has no
``last_user_edit_at`` column, so this is effectively "last sync wins."
Same property the pre-fix two-field path had; not worse. A proper LWW
gate would need a schema migration.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture
async def fresh_db():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, ApiKey
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


def test_push_payload_includes_operator_settable_fields():
    """The push-payload builder must include every operator-settable
    field. Source-level guard so a future field addition doesn't
    silently regress."""
    src = Path("app/cluster/manager.py").read_text()
    # Locate the api_keys list-comprehension
    idx = src.index("keys = [")
    block = src[idx:idx + 3000]
    for field in (
        "semantic_cache_enabled",
        "daily_soft_cap_usd",
        "daily_hard_cap_usd",
        "hourly_cap_usd",
        "rate_limit_tier",
        "caller_memory_ttl_days",
        "lmrh_polling_rpm",
        "lmrh_quotes_rpm",
        # v5.0.0 — compliance per-key policy fields
        "blocked_companies",
        "allowed_paths",
        "debug_echo_enabled",
    ):
        assert field in block, f"push payload missing {field}"


# ── Apply handler — behavioral ───────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_cache_enabled_propagates_on_update(fresh_db):
    """The F3 trigger case: a peer sends ``semantic_cache_enabled=1``
    for an existing local row; the local row must update."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="key-test-1", name="hub-test",
            key_hash="hash-1", key_prefix="llmp-t1",
            enabled=True, semantic_cache_enabled=False,
        ))
        await db.commit()

        await apply_sync(db, {"api_keys": [{
            "id": "key-test-1", "name": "hub-test",
            "key_hash": "hash-1", "key_prefix": "llmp-t1",
            "semantic_cache_enabled": True,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "hash-1")
        )).scalar_one()
        assert row.semantic_cache_enabled is True, (
            "semantic_cache_enabled=True from peer did not propagate"
        )


@pytest.mark.asyncio
async def test_budget_caps_propagate_on_update(fresh_db):
    """daily_soft_cap_usd / daily_hard_cap_usd / hourly_cap_usd."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="key-test-2", name="caps-test",
            key_hash="hash-2", key_prefix="llmp-t2",
            enabled=True,
        ))
        await db.commit()

        await apply_sync(db, {"api_keys": [{
            "id": "key-test-2", "name": "caps-test",
            "key_hash": "hash-2", "key_prefix": "llmp-t2",
            "daily_soft_cap_usd": 5.0,
            "daily_hard_cap_usd": 25.0,
            "hourly_cap_usd": 2.5,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "hash-2")
        )).scalar_one()
        assert row.daily_soft_cap_usd == 5.0
        assert row.daily_hard_cap_usd == 25.0
        assert row.hourly_cap_usd == 2.5


@pytest.mark.asyncio
async def test_caller_memory_ttl_propagates_on_update(fresh_db):
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="key-test-3", name="ttl-test",
            key_hash="hash-3", key_prefix="llmp-t3",
            enabled=True,
        ))
        await db.commit()

        await apply_sync(db, {"api_keys": [{
            "id": "key-test-3", "name": "ttl-test",
            "key_hash": "hash-3", "key_prefix": "llmp-t3",
            "caller_memory_ttl_days": 14,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "hash-3")
        )).scalar_one()
        assert row.caller_memory_ttl_days == 14


@pytest.mark.asyncio
async def test_enabled_flag_propagates_on_update(fresh_db):
    """Disabling on one node must propagate to peers."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="key-test-4", name="enable-test",
            key_hash="hash-4", key_prefix="llmp-t4",
            enabled=True,
        ))
        await db.commit()

        await apply_sync(db, {"api_keys": [{
            "id": "key-test-4", "name": "enable-test",
            "key_hash": "hash-4", "key_prefix": "llmp-t4",
            "enabled": False,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "hash-4")
        )).scalar_one()
        assert row.enabled is False


@pytest.mark.asyncio
async def test_membership_test_doesnt_clobber_missing_fields(fresh_db):
    """A payload omitting a field (older-build peer) must NOT clobber
    the local value with None."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="key-test-5", name="omit-test",
            key_hash="hash-5", key_prefix="llmp-t5",
            enabled=True, semantic_cache_enabled=True,
            daily_hard_cap_usd=100.0,
        ))
        await db.commit()

        # Payload that omits semantic_cache_enabled + caps entirely
        # (e.g. a pre-v4.4.18 peer)
        await apply_sync(db, {"api_keys": [{
            "id": "key-test-5", "name": "omit-test",
            "key_hash": "hash-5", "key_prefix": "llmp-t5",
            "spending_cap_usd": 50.0,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "hash-5")
        )).scalar_one()
        # Locally-set values preserved
        assert row.semantic_cache_enabled is True
        assert row.daily_hard_cap_usd == 100.0
        # The explicitly-present field IS updated
        assert row.spending_cap_usd == 50.0
