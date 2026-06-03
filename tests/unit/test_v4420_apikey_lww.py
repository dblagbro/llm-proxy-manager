"""v4.4.20 — api_keys cluster-sync gains a proper LWW gate.

The v4.4.18 follow-up: api_keys sync was effectively "last sync wins"
because the table had no per-row admin-edit timestamp. v4.4.20 adds
``last_user_edit_at`` (mirror of the v3.0.11 ``Provider.last_user_edit_at``
pattern), bumps it on every operator PATCH, includes it in the push
payload, and gates the apply path on it.

Branch matrix tested:
  - both stamped + peer > local → accept
  - both stamped + peer == local → tie → keep local (no updated_at
    fallback on api_keys, so the provider "fall through to updated_at"
    branch reduces to "keep local"; same anti-ping-pong property as
    v3.0.63's strict-greater fix)
  - both stamped + peer < local → reject
  - local stamped + peer unstamped → keep local (conservative)
  - peer stamped + local unstamped → accept (legacy upgrade path)
  - neither stamped → accept (matches pre-LWW behavior of v4.4.18/19)

The "stamp peer's value on accept" behavior is also exercised so that
subsequent sync cycles converge instead of ping-ponging.
"""
from __future__ import annotations

from pathlib import Path
import time

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture
async def fresh_db():
    """Drop + recreate so a cached DB file from prior test runs picks
    up new columns. v4.4.18's variant assumed all columns already
    existed; v4.4.20 adds ``last_user_edit_at``, so we must rebuild
    the api_keys table — ``create_all`` alone is a no-op when the
    table already exists with the old schema."""
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


# ── Model + push-payload surface guards ──────────────────────────────


def test_model_has_last_user_edit_at():
    """Source guard so a future model rewrite doesn't drop the LWW gate."""
    src = Path("app/models/db_apikey.py").read_text()
    assert "last_user_edit_at" in src, "ApiKey lost its LWW column"


def test_push_payload_includes_last_user_edit_at():
    """Manager push payload must carry the LWW stamp."""
    src = Path("app/cluster/manager.py").read_text()
    idx = src.index("keys = [")
    block = src[idx:idx + 3500]
    assert "last_user_edit_at" in block, (
        "push payload missing last_user_edit_at — peers will never get the "
        "LWW stamp and stay on legacy last-sync-wins forever"
    )


def test_patch_endpoint_bumps_last_user_edit_at():
    """Source guard — the PATCH path must stamp the row. If a future
    refactor moves the assignment out of update_key the LWW gate goes
    half-dead (peers gate but operator never writes)."""
    src = Path("app/api/apikeys.py").read_text()
    idx = src.index("async def update_key")
    # v5.0.0: window widened from 2500 → 4500 because Agent 5 added
    # compliance fields (blocked_companies / allowed_paths /
    # debug_echo_enabled + policy-change audit emission) inline in
    # ``update_key``, pushing the LWW stamp assignment past the old 2500
    # boundary. The intent of the guard — stamp still inside ``update_key`` —
    # is unchanged.
    block = src[idx:idx + 4500]
    assert "last_user_edit_at" in block, (
        "PATCH endpoint no longer stamps last_user_edit_at — operator "
        "edits will never propagate under the LWW gate"
    )


def test_patch_uses_walltime_for_stamp():
    """The stamp must be wall-clock (cross-node comparable). A
    ``time.monotonic()`` would silently break LWW comparisons across
    peers since each node's monotonic clock has a different epoch."""
    src = Path("app/api/apikeys.py").read_text()
    idx = src.index("async def update_key")
    # v5.0.0: window widened from 2500 → 4500 because Agent 5 added
    # compliance fields (blocked_companies / allowed_paths /
    # debug_echo_enabled + policy-change audit emission) inline in
    # ``update_key``, pushing the LWW stamp assignment past the old 2500
    # boundary. The intent of the guard — stamp still inside ``update_key`` —
    # is unchanged.
    block = src[idx:idx + 4500]
    # Heuristic: the bump line uses ``time.time()``; reject monotonic.
    assert "time.time()" in block, "expected wall-clock stamp"
    assert "time.monotonic()" not in block, (
        "monotonic clock would break cross-node LWW comparisons"
    )


# ── Apply-handler LWW branch matrix ──────────────────────────────────


@pytest.mark.asyncio
async def test_lww_accepts_strictly_newer_peer(fresh_db):
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="k-newer", name="newer", key_hash="h-newer", key_prefix="lp-nw",
            enabled=True, daily_hard_cap_usd=10.0,
            last_user_edit_at=1000.0,
        ))
        await db.commit()

        await apply_sync(db, {"api_keys": [{
            "id": "k-newer", "name": "newer", "key_hash": "h-newer",
            "key_prefix": "lp-nw",
            "daily_hard_cap_usd": 50.0,
            "last_user_edit_at": 2000.0,  # peer stamp newer
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "h-newer")
        )).scalar_one()
        assert row.daily_hard_cap_usd == 50.0, "newer peer edit should win"
        assert row.last_user_edit_at == 2000.0, "stamp must mirror peer's"


@pytest.mark.asyncio
async def test_lww_rejects_strictly_older_peer(fresh_db):
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="k-older", name="older", key_hash="h-older", key_prefix="lp-ol",
            enabled=True, daily_hard_cap_usd=99.0,
            last_user_edit_at=5000.0,
        ))
        await db.commit()

        await apply_sync(db, {"api_keys": [{
            "id": "k-older", "name": "older", "key_hash": "h-older",
            "key_prefix": "lp-ol",
            "daily_hard_cap_usd": 1.0,
            "last_user_edit_at": 3000.0,  # peer stamp older
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "h-older")
        )).scalar_one()
        assert row.daily_hard_cap_usd == 99.0, (
            "older peer edit must NOT clobber newer local"
        )
        assert row.last_user_edit_at == 5000.0, (
            "local stamp must not regress when peer is rejected"
        )


@pytest.mark.asyncio
async def test_lww_tie_keeps_local(fresh_db):
    """Anti-ping-pong: equal stamps → keep local. Mirrors the v3.0.63
    strict-greater fix on the provider side."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="k-tie", name="tie", key_hash="h-tie", key_prefix="lp-ti",
            enabled=True, daily_hard_cap_usd=7.0,
            last_user_edit_at=4000.0,
        ))
        await db.commit()

        await apply_sync(db, {"api_keys": [{
            "id": "k-tie", "name": "tie", "key_hash": "h-tie",
            "key_prefix": "lp-ti",
            "daily_hard_cap_usd": 999.0,
            "last_user_edit_at": 4000.0,  # exact tie
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "h-tie")
        )).scalar_one()
        assert row.daily_hard_cap_usd == 7.0, (
            "tie must NOT accept peer — that's the v3.0.63 ping-pong shape"
        )


@pytest.mark.asyncio
async def test_lww_local_stamped_peer_unstamped_keeps_local(fresh_db):
    """Conservative branch: real operator edit on local, peer payload
    has no stamp (legacy peer or background-only mutation). Keep local."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="k-lhs", name="lhs", key_hash="h-lhs", key_prefix="lp-lh",
            enabled=True, daily_hard_cap_usd=42.0,
            last_user_edit_at=1500.0,
        ))
        await db.commit()

        # Payload omits last_user_edit_at entirely
        await apply_sync(db, {"api_keys": [{
            "id": "k-lhs", "name": "lhs", "key_hash": "h-lhs",
            "key_prefix": "lp-lh",
            "daily_hard_cap_usd": 1.0,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "h-lhs")
        )).scalar_one()
        assert row.daily_hard_cap_usd == 42.0, (
            "unstamped peer payload must not clobber a real local edit"
        )
        assert row.last_user_edit_at == 1500.0


@pytest.mark.asyncio
async def test_lww_peer_stamped_local_unstamped_accepts(fresh_db):
    """Upgrade path: legacy local row (last_user_edit_at NULL); peer
    has a fresh stamp. Accept and adopt the stamp."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="k-rhs", name="rhs", key_hash="h-rhs", key_prefix="lp-rh",
            enabled=True, daily_hard_cap_usd=10.0,
            last_user_edit_at=None,  # legacy row
        ))
        await db.commit()

        await apply_sync(db, {"api_keys": [{
            "id": "k-rhs", "name": "rhs", "key_hash": "h-rhs",
            "key_prefix": "lp-rh",
            "daily_hard_cap_usd": 25.0,
            "last_user_edit_at": 9999.0,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "h-rhs")
        )).scalar_one()
        assert row.daily_hard_cap_usd == 25.0, "fresh peer edit should win on legacy local"
        assert row.last_user_edit_at == 9999.0, "stamp must be adopted"


@pytest.mark.asyncio
async def test_lww_neither_stamped_legacy_last_sync_wins(fresh_db):
    """Both legacy — preserve pre-v4.4.20 behavior (last sync wins)
    so a mixed-version fleet doesn't deadlock pending the upgrade."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        db.add(ApiKey(
            id="k-neither", name="neither", key_hash="h-neither",
            key_prefix="lp-ne",
            enabled=True, daily_hard_cap_usd=11.0,
        ))
        await db.commit()

        await apply_sync(db, {"api_keys": [{
            "id": "k-neither", "name": "neither", "key_hash": "h-neither",
            "key_prefix": "lp-ne",
            "daily_hard_cap_usd": 22.0,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "h-neither")
        )).scalar_one()
        assert row.daily_hard_cap_usd == 22.0, (
            "legacy-both path must remain last-sync-wins for v4.4.18/19 compatibility"
        )


@pytest.mark.asyncio
async def test_insert_path_carries_peer_stamp(fresh_db):
    """When materializing a row a peer sent us (no local match),
    record the peer's stamp so the very next sync round-trip benefits
    from the LWW gate instead of going through legacy."""
    from app.cluster.sync import apply_sync
    from app.models.db import ApiKey

    async with fresh_db() as db:
        await apply_sync(db, {"api_keys": [{
            "id": "k-fresh", "name": "fresh", "key_hash": "h-fresh",
            "key_prefix": "lp-fr",
            "key_type": "standard", "enabled": True,
            "last_user_edit_at": 1234.5,
        }]})
        await db.commit()

        row = (await db.execute(
            select(ApiKey).where(ApiKey.key_hash == "h-fresh")
        )).scalar_one()
        assert row.last_user_edit_at == 1234.5
