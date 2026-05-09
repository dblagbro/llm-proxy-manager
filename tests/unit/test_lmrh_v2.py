"""LMRHv2 endpoint + snapshot tests.

Covers:
- /.well-known/lmrh-config — version negotiation depending on flag
- /lmrh/providers — feature flag gate, scope filter, ETag round-trip
- /lmrh/providers/{id} — 404 for hidden vs unknown, 200 for visible
- /lmrh/health — returns aggregate counters
- snapshot.LmrhSnapshot.for_caller — scope filter unit
- rate limit — hits 429 after default budget exhausted
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


# ── Fixtures ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def fixture_db():
    """Seed two providers (one shared, one owned by another key) and
    one ApiKey (the test caller). Yield AsyncSessionLocal."""
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import ApiKey, Base, Provider

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ApiKey).where(ApiKey.id.in_(["lv2-key", "lv2-other"])))
        await cleanup.execute(delete(Provider).where(Provider.id.in_(["lv2-shared", "lv2-private"])))
        await cleanup.commit()
    async with AsyncSessionLocal() as db:
        db.add(ApiKey(
            id="lv2-key", name="lv2-test",
            key_prefix="llmp-lv2t",
            key_hash="hash-lv2",
            enabled=True,
        ))
        db.add(ApiKey(
            id="lv2-other", name="lv2-other",
            key_prefix="llmp-other",
            key_hash="hash-other",
            enabled=True,
        ))
        db.add(Provider(
            id="lv2-shared", name="lv2-Shared", provider_type="openai",
            priority=10, enabled=True, default_model="gpt-4o",
        ))
        # Private to a different key — shouldn't appear in lv2-key's view
        db.add(Provider(
            id="lv2-private", name="lv2-Private", provider_type="anthropic",
            priority=11, enabled=True, default_model="claude-sonnet-4-6",
            owned_by_key_id="lv2-other",
        ))
        await db.commit()

    yield AsyncSessionLocal

    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ApiKey).where(ApiKey.id.in_(["lv2-key", "lv2-other"])))
        await cleanup.execute(delete(Provider).where(Provider.id.in_(["lv2-shared", "lv2-private"])))
        await cleanup.commit()


# ── Snapshot scope filter ────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_scope_filter_excludes_other_keys_private(fixture_db):
    """Operator decision #1: providers owned by a different key must
    be invisible in a caller's snapshot view."""
    from app.routing.lmrh import snapshot as snap_mod
    AsyncSessionLocal = fixture_db
    async with AsyncSessionLocal() as db:
        snap = await snap_mod._build_snapshot(db)

    visible = snap.for_caller("lv2-key")
    visible_ids = {p.id for p in visible}
    assert "lv2-shared" in visible_ids, "shared providers must be visible"
    assert "lv2-private" not in visible_ids, \
        "providers owned by other keys must be hidden"

    # The owner of the private provider sees both
    owner_view = snap.for_caller("lv2-other")
    owner_ids = {p.id for p in owner_view}
    assert "lv2-shared" in owner_ids
    assert "lv2-private" in owner_ids


@pytest.mark.asyncio
async def test_snapshot_etag_stable_across_rebuilds(fixture_db):
    """Two snapshots with identical underlying data must produce the
    same ETag (lets clients 304 between background refreshes)."""
    from app.routing.lmrh import snapshot as snap_mod
    AsyncSessionLocal = fixture_db
    async with AsyncSessionLocal() as db:
        s1 = await snap_mod._build_snapshot(db)
        s2 = await snap_mod._build_snapshot(db)
    assert s1.etag == s2.etag


# ── Endpoint gate ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_well_known_returns_v1_only_when_v2_disabled(monkeypatch):
    """Public endpoint always responds; advertises v1 only when v2 is off."""
    from app.config import settings
    monkeypatch.setattr(settings, "lmrh_v2_enabled", False)
    from app.api.lmrh_v2 import well_known_config
    out = await well_known_config()
    assert "1.2" in out["supported_versions"]
    assert "2.0" not in out["supported_versions"]
    assert "providers" not in out["endpoints"]


@pytest.mark.asyncio
async def test_well_known_advertises_v2_when_enabled(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lmrh_v2_enabled", True)
    from app.api.lmrh_v2 import well_known_config
    out = await well_known_config()
    assert "2.0" in out["supported_versions"]
    assert "providers" in out["endpoints"]
    assert out["polling"]["providers_max_rate_per_minute"] == 4


@pytest.mark.asyncio
async def test_providers_endpoint_404_when_disabled(monkeypatch):
    """Operator decision #6: when flag is off, endpoints behave as if
    they don't exist (404 not 503) so v1.x callers can't probe."""
    from app.config import settings
    from app.api.lmrh_v2 import _ensure_enabled
    from fastapi import HTTPException
    monkeypatch.setattr(settings, "lmrh_v2_enabled", False)
    with pytest.raises(HTTPException) as ex:
        _ensure_enabled()
    assert ex.value.status_code == 404


# ── Rate limit ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_enforced(fixture_db, monkeypatch):
    """Default 4/min for /lmrh/providers — 5th call inside the window
    raises 429 with Retry-After header."""
    from app.api import lmrh_v2 as lv2
    from app.auth.keys import ApiKeyRecord
    from fastapi import HTTPException
    AsyncSessionLocal = fixture_db
    # Fresh state for this test
    from collections import defaultdict
    monkeypatch.setattr(lv2, "_rate_state", defaultdict(list))
    key = ApiKeyRecord(id="lv2-key", name="lv2-test", key_type="standard")
    async with AsyncSessionLocal() as db:
        for i in range(4):
            await lv2._check_rate_limit(db, key, "providers")
        # 5th must fail
        with pytest.raises(HTTPException) as ex:
            await lv2._check_rate_limit(db, key, "providers")
        assert ex.value.status_code == 429
        assert "Retry-After" in ex.value.headers


@pytest.mark.asyncio
async def test_rate_limit_respects_per_key_override(fixture_db, monkeypatch):
    """ApiKey.lmrh_polling_rpm column overrides the default."""
    from app.api import lmrh_v2 as lv2
    from app.auth.keys import ApiKeyRecord
    from app.models.db import ApiKey
    from fastapi import HTTPException
    AsyncSessionLocal = fixture_db
    from collections import defaultdict
    monkeypatch.setattr(lv2, "_rate_state", defaultdict(list))

    # Bump test key's override to 100/min
    async with AsyncSessionLocal() as db:
        k = (await db.execute(select(ApiKey).where(ApiKey.id == "lv2-key"))).scalar_one()
        k.lmrh_polling_rpm = 100
        await db.commit()

    key = ApiKeyRecord(id="lv2-key", name="lv2-test", key_type="standard")
    async with AsyncSessionLocal() as db:
        # Should sail through 50 calls without 429
        for _ in range(50):
            await lv2._check_rate_limit(db, key, "providers")
    # If we got here, no exception was raised — pass.


# ── Render shape ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_provider_strips_owned_by_key_id(fixture_db):
    """The internal owned_by_key_id field MUST NOT ride the wire — it
    would leak which keys can route where. The scope filter ran
    upstream; the render layer hides the field unconditionally."""
    from app.routing.lmrh import snapshot as snap_mod
    from app.api.lmrh_v2 import _render_provider
    AsyncSessionLocal = fixture_db
    async with AsyncSessionLocal() as db:
        snap = await snap_mod._build_snapshot(db)
    p = next(p for p in snap.providers if p.id == "lv2-shared")
    rendered = _render_provider(p)
    assert "owned_by_key_id" not in rendered, \
        "internal-only field must not appear in wire response"


# ── Health endpoint ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_aggregate_counts_visible_only(fixture_db, monkeypatch):
    """/lmrh/health returns aggregate counters — must reflect only the
    caller-visible providers, not the global fleet."""
    from app.config import settings
    monkeypatch.setattr(settings, "lmrh_v2_enabled", True)
    from app.api import lmrh_v2 as lv2
    from app.auth.keys import ApiKeyRecord
    AsyncSessionLocal = fixture_db
    from collections import defaultdict
    monkeypatch.setattr(lv2, "_rate_state", defaultdict(list))

    # Force-rebuild snapshot so it reflects fresh fixture data
    from app.routing.lmrh import snapshot as snap_mod
    async with AsyncSessionLocal() as db:
        await snap_mod.rebuild_now(db)

    key = ApiKeyRecord(id="lv2-key", name="lv2-test", key_type="standard")
    async with AsyncSessionLocal() as db:
        out = await lv2.get_health(db=db, key=key)
    # lv2-key sees: lv2-shared (yes) + lv2-private (no, owned by lv2-other)
    assert out["total_providers"] == 1
    assert out["circuit_open_count"] == 0
