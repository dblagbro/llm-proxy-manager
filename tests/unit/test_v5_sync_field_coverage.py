"""v5.0.0 — compliance-related cluster-sync field coverage.

Same class of guard as v4.4.18 / v4.4.25 for the new v5.0.0 fields:

  * ``ApiKey.blocked_companies`` round-trips on UPDATE + INSERT.
  * ``Provider.owner_company`` round-trips on UPDATE + INSERT.
  * ``compliance_events`` appends on first sight, dedupes on duplicate
    ``audit_id`` (the unique business key).
  * A provider ``owner_company`` change invalidates the blocklist cache
    (router pre-filter reads ``provider.owner_company`` per request, so a
    stale cached blocklist set would let a banned provider slip through
    until the 30s TTL).
"""
from __future__ import annotations

import time

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture
async def fresh_db():
    """Drop + recreate api_keys / providers / compliance_events / policy
    changes so prior runs' state doesn't leak in."""
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import (
        ApiKey, Provider, ComplianceEvent, CompliancePolicyChange, Base,
    )
    async with engine.begin() as conn:
        for table in (
            ComplianceEvent.__table__,
            CompliancePolicyChange.__table__,
            ApiKey.__table__,
            Provider.__table__,
        ):
            await conn.run_sync(table.drop, checkfirst=True)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        for model in (ComplianceEvent, CompliancePolicyChange, ApiKey, Provider):
            await cleanup.execute(delete(model))
        await cleanup.commit()
    yield AsyncSessionLocal
    async with AsyncSessionLocal() as cleanup:
        for model in (ComplianceEvent, CompliancePolicyChange, ApiKey, Provider):
            await cleanup.execute(delete(model))
        await cleanup.commit()


# ── api_keys: blocked_companies + allowed_paths + debug_echo round-trip ──


@pytest.mark.asyncio
async def test_blocked_companies_propagates_on_update(fresh_db):
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="k-comp-1", name="comp-1",
            key_hash="h-comp-1", key_prefix="llmp-c1",
            enabled=True,
        ))
        await db.commit()

        await apply_sync(db, {"api_keys": [{
            "id": "k-comp-1", "name": "comp-1",
            "key_hash": "h-comp-1", "key_prefix": "llmp-c1",
            "blocked_companies": ["anthropic"],
            "allowed_paths": ["/v1/messages"],
            "debug_echo_enabled": True,
            "last_user_edit_at": time.time(),
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "h-comp-1")
        )).scalar_one()
        assert row.blocked_companies == ["anthropic"]
        assert row.allowed_paths == ["/v1/messages"]
        assert row.debug_echo_enabled is True


@pytest.mark.asyncio
async def test_blocked_companies_propagates_on_insert(fresh_db):
    """BUG-084-style coverage: a fresh peer-imported key carrying
    compliance fields must materialize them on insert, not at defaults."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        await apply_sync(db, {"api_keys": [{
            "id": "k-comp-2", "name": "comp-2",
            "key_hash": "h-comp-2", "key_prefix": "llmp-c2",
            "key_type": "standard", "enabled": True,
            "blocked_companies": ["anthropic", "openai"],
            "allowed_paths": ["/v1/messages", "/v1/chat/completions"],
            "debug_echo_enabled": False,
            "last_user_edit_at": time.time(),
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "h-comp-2")
        )).scalar_one()
        assert row.blocked_companies == ["anthropic", "openai"]
        assert row.allowed_paths == ["/v1/messages", "/v1/chat/completions"]


# ── providers: owner_company round-trip ──


@pytest.mark.asyncio
async def test_provider_owner_company_propagates_on_update(fresh_db):
    from app.cluster.sync import apply_sync
    from app.models.db import Provider

    async with fresh_db() as db:
        db.add(Provider(
            id="p-1", name="p-1", provider_type="anthropic",
            priority=10, enabled=True,
        ))
        await db.commit()

        await apply_sync(db, {"providers": [{
            "id": "p-1", "name": "p-1", "provider_type": "anthropic",
            "owner_company": "anthropic",
            "last_user_edit_at": time.time(),
        }]})
        await db.commit()

        row = (await db.execute(
            select(Provider).where(Provider.id == "p-1")
        )).scalar_one()
        assert row.owner_company == "anthropic"


@pytest.mark.asyncio
async def test_provider_owner_company_propagates_on_insert(fresh_db):
    from app.cluster.sync import apply_sync
    from app.models.db import Provider

    async with fresh_db() as db:
        await apply_sync(db, {"providers": [{
            "id": "p-2", "name": "p-2", "provider_type": "openai",
            "owner_company": "openai",
            "last_user_edit_at": time.time(),
        }]})
        await db.commit()

        row = (await db.execute(
            select(Provider).where(Provider.id == "p-2")
        )).scalar_one()
        assert row.owner_company == "openai"


@pytest.mark.asyncio
async def test_provider_owner_company_invalidates_blocklist_cache(fresh_db):
    """A provider owner_company change must clear the blocklist cache —
    otherwise the next request keeps the stale provider filter for up to
    30s and a banned provider can slip through."""
    from app.cluster.sync import apply_sync
    from app.compliance import policy
    from app.models.db import Provider

    async with fresh_db() as db:
        db.add(Provider(
            id="p-cache", name="p-cache", provider_type="openai",
            owner_company="openai", priority=10, enabled=True,
        ))
        await db.commit()

        # Prime the cache with a sentinel entry.
        policy._BLOCKLIST_CACHE["sentinel-key"] = (
            time.monotonic() + 60.0, {"anthropic"},
        )
        assert "sentinel-key" in policy._BLOCKLIST_CACHE

        await apply_sync(db, {"providers": [{
            "id": "p-cache", "name": "p-cache",
            "provider_type": "openai",
            "owner_company": "anthropic",  # ownership reclassified
            "last_user_edit_at": time.time(),
        }]})
        await db.commit()

        # Provider update must invalidate the whole cache (None target).
        assert "sentinel-key" not in policy._BLOCKLIST_CACHE


# ── compliance_events: dedupe on audit_id ──


@pytest.mark.asyncio
async def test_compliance_event_inserted_on_first_sight(fresh_db):
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey, ComplianceEvent

    async with fresh_db() as db:
        db.add(ApiKey(
            id="k-evt", name="evt", key_hash="h-evt", key_prefix="llmp-e",
            enabled=True,
        ))
        await db.commit()

        await apply_sync(db, {"compliance_events": [{
            "audit_id": "comp_01JX_NEW",
            "api_key_id": "k-evt",
            "event_type": "model_substitution",
            "reason_code": "api-key-policy:blocked-company:anthropic",
            "http_status": 200,
            "blocked_company": "anthropic",
            "requested_at": "2026-06-03T12:00:00+00:00",
        }]})
        await db.commit()

        rows = (await db.execute(
            select(ComplianceEvent).where(ComplianceEvent.audit_id == "comp_01JX_NEW")
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].event_type == "model_substitution"
        assert rows[0].blocked_company == "anthropic"


@pytest.mark.asyncio
async def test_compliance_event_duplicate_audit_id_skipped(fresh_db):
    """Receiver must dedupe on audit_id — pushing the same row twice
    leaves exactly one DB row (idempotent on the re-push window)."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey, ComplianceEvent

    async with fresh_db() as db:
        db.add(ApiKey(
            id="k-dup", name="dup", key_hash="h-dup", key_prefix="llmp-d",
            enabled=True,
        ))
        await db.commit()

        # Pre-seed: row already present locally.
        db.add(ComplianceEvent(
            audit_id="comp_01JX_DUP",
            api_key_id="k-dup",
            event_type="model_substitution",
            reason_code="api-key-policy:blocked-company:anthropic",
            http_status=200,
        ))
        await db.commit()

        await apply_sync(db, {"compliance_events": [{
            "audit_id": "comp_01JX_DUP",
            "api_key_id": "k-dup",
            "event_type": "model_substitution",
            "reason_code": "api-key-policy:blocked-company:anthropic",
            "http_status": 200,
        }]})
        await db.commit()

        rows = (await db.execute(
            select(ComplianceEvent).where(ComplianceEvent.audit_id == "comp_01JX_DUP")
        )).scalars().all()
        assert len(rows) == 1, "duplicate audit_id should be deduped"


@pytest.mark.asyncio
async def test_compliance_policy_change_dedup_on_id(fresh_db):
    from app.cluster.sync import apply_sync
    from app.models.db import CompliancePolicyChange

    async with fresh_db() as db:
        # Push twice; the second must be a no-op.
        payload = {
            "compliance_policy_changes": [{
                "policy_change_id": "ppc_01JX_TEST",
                "changed_at": "2026-06-03T12:00:00+00:00",
                "changed_by_user_id": "admin",
                "scope": "system",
                "target_id": None,
                "before_state": "{}",
                "after_state": '{"blocked_companies": ["anthropic"]}',
                "reason": "gov compliance",
                "applied_to_peers": '[{"peer":"alpha","acked_at":"2026-06-03T12:00:01Z"}]',
                "pending_peers": "[]",
                "cluster_sync_status": "fully-acked",
            }]
        }
        await apply_sync(db, payload)
        await db.commit()
        await apply_sync(db, payload)
        await db.commit()

        rows = (await db.execute(
            select(CompliancePolicyChange)
            .where(CompliancePolicyChange.policy_change_id == "ppc_01JX_TEST")
        )).scalars().all()
        assert len(rows) == 1
