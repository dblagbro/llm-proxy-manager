"""Cluster-sync LWW tests covering the v3.2.7 tie-break fall-through.

Background: ``apply_sync`` resolves conflicts by comparing a row's
``last_user_edit_at`` (Unix float, set by admin-driven edits) and falling
back to ``updated_at`` LWW for legacy rows that don't carry the user-edit
stamp. v3.0.63 made the user-edit comparison STRICT-greater to break a
ping-pong. v3.2.7 adds: when the user-edit stamps are EQUAL (real tie),
fall through to ``updated_at`` LWW so direct DB writes that bumped only
``updated_at`` (e.g. operator scripts, cluster-cascade flushes) still
propagate.
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture
async def fresh_db():
    """Reuse the module-level engine, ensure schema exists, and clean up
    any Provider rows from prior tests. A fresh-engine-per-test fixture
    runs into SQLAlchemy's lazy-binding rules — the app imports the
    engine once at startup, so swapping DATABASE_URL mid-run leaves the
    engine pinned to whatever path was active first.
    """
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, Provider

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(Provider))
        await cleanup.commit()
    yield AsyncSessionLocal
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(Provider))
        await cleanup.commit()


def _iso(dt: datetime) -> str:
    """ISO8601 with explicit UTC offset, matching what the cluster
    payload builder emits."""
    return dt.astimezone(timezone.utc).isoformat()


def _provider_payload(
    *, id, name="p", provider_type="openai", priority=10, enabled=True,
    extra_config=None, last_user_edit_at=None, updated_at, deleted_at=None,
):
    return {
        "id": id,
        "name": name,
        "provider_type": provider_type,
        "priority": priority,
        "enabled": enabled,
        "extra_config": extra_config or {},
        "last_user_edit_at": last_user_edit_at,
        "updated_at": _iso(updated_at),
        "deleted_at": _iso(deleted_at) if deleted_at else None,
        "timeout_sec": 30,
        "exclude_from_tool_requests": False,
        "hold_down_sec": None,
        "failure_threshold": None,
    }


@pytest.mark.asyncio
async def test_user_edit_strict_greater_blocks_revert(fresh_db):
    """v3.0.63: a peer payload carrying the SAME last_user_edit_at as the
    local row must NOT overwrite local state. Sanity-check that the
    v3.2.7 fall-through doesn't break this when updated_at is also tied
    (genuinely-converged state)."""
    from app.cluster.sync import apply_sync
    from app.models.db import Provider

    now = datetime.now(timezone.utc)
    async with fresh_db() as db:
        # Seed local: priority=5, last_user_edit_at=100, updated_at=now
        db.add(Provider(
            id="p1", name="local", provider_type="openai",
            priority=5, enabled=True, extra_config={},
            last_user_edit_at=100.0, updated_at=now,
        ))
        await db.commit()

        # Peer payload: SAME last_user_edit_at, SAME updated_at, but priority=99
        # (an attempted revert). Must be rejected.
        await apply_sync(db, {"providers": [_provider_payload(
            id="p1", name="local", priority=99,
            last_user_edit_at=100.0, updated_at=now,
        )]})
        await db.commit()

        local = (await db.execute(select(Provider).where(Provider.id == "p1"))).scalar_one()
        assert local.priority == 5, "tie + tie should keep local (anti-ping-pong)"


@pytest.mark.asyncio
async def test_user_edit_tie_with_newer_updated_at_accepts(fresh_db):
    """v3.2.7: when last_user_edit_at is equal but the peer's updated_at
    is strictly newer, the peer's state propagates. This is the bug the
    fix addresses — a direct DB write bumped updated_at but not
    last_user_edit_at, so the v3.0.63 strict-greater check blocked the
    propagation entirely."""
    from app.cluster.sync import apply_sync
    from app.models.db import Provider

    now = datetime.now(timezone.utc)
    later = now + timedelta(seconds=30)
    async with fresh_db() as db:
        db.add(Provider(
            id="p2", name="grok-web", provider_type="grok-web",
            priority=5, enabled=True,
            extra_config={"bridge_url": "http://internal-only-bad"},
            last_user_edit_at=200.0, updated_at=now,
        ))
        await db.commit()

        # Peer payload: SAME last_user_edit_at (background mutation didn't
        # bump it), but updated_at is NEWER and extra_config has changed.
        await apply_sync(db, {"providers": [_provider_payload(
            id="p2", name="grok-web", provider_type="grok-web",
            priority=5,
            extra_config={"bridge_url": "https://www.voipguru.org/grok-bridge"},
            last_user_edit_at=200.0, updated_at=later,
        )]})
        await db.commit()

        local = (await db.execute(select(Provider).where(Provider.id == "p2"))).scalar_one()
        assert local.extra_config["bridge_url"] == "https://www.voipguru.org/grok-bridge", \
            "tie on user-edit + newer updated_at should accept peer state"


@pytest.mark.asyncio
async def test_peer_user_edit_newer_accepts(fresh_db):
    """Existing v3.0.63 path: peer's last_user_edit_at strictly greater than
    local's wins regardless of updated_at."""
    from app.cluster.sync import apply_sync
    from app.models.db import Provider

    now = datetime.now(timezone.utc)
    async with fresh_db() as db:
        db.add(Provider(
            id="p3", name="x", provider_type="openai",
            priority=10, enabled=True, extra_config={},
            last_user_edit_at=100.0, updated_at=now,
        ))
        await db.commit()

        await apply_sync(db, {"providers": [_provider_payload(
            id="p3", name="x", priority=99,
            last_user_edit_at=200.0, updated_at=now,  # same updated_at, newer user-edit
        )]})
        await db.commit()

        local = (await db.execute(select(Provider).where(Provider.id == "p3"))).scalar_one()
        assert local.priority == 99


@pytest.mark.asyncio
async def test_peer_user_edit_older_rejects(fresh_db):
    """Symmetric: peer's last_user_edit_at strictly less than local's must
    NOT propagate even if peer's updated_at is newer (the v3.0.11 design —
    real admin edits beat background bumps from peers)."""
    from app.cluster.sync import apply_sync
    from app.models.db import Provider

    now = datetime.now(timezone.utc)
    later = now + timedelta(seconds=60)
    async with fresh_db() as db:
        db.add(Provider(
            id="p4", name="x", provider_type="openai",
            priority=42, enabled=True, extra_config={},
            last_user_edit_at=300.0, updated_at=now,
        ))
        await db.commit()

        await apply_sync(db, {"providers": [_provider_payload(
            id="p4", name="x", priority=1,
            last_user_edit_at=200.0,  # OLDER user-edit
            updated_at=later,         # but newer updated_at (background bump)
        )]})
        await db.commit()

        local = (await db.execute(select(Provider).where(Provider.id == "p4"))).scalar_one()
        assert local.priority == 42, "older user-edit must not overwrite even with newer updated_at"


# ── v4.4.2 BUG-053 — tombstone propagation regression guard ─────────


@pytest.mark.asyncio
async def test_bug053_tombstone_propagates_when_local_updated_at_is_newer(fresh_db):
    """v4.4.2 BUG-053. The pre-fix gate was
    ``peer_deleted_at >= local_updated`` — when background activity
    (sync cycles, OAuth refresh, scrapes) bumped local.updated_at
    past the originator's deleted_at, the tombstone never propagated.
    Live evidence 2026-05-20: skew-from-new-41a9d6 tombstoned on www1
    at 03:33 UTC, still active on www2 + c1conv 18 hours later
    despite ongoing sync cycles.

    Fix: tombstone always wins when local doesn't have one. Tombstones
    are terminal in this app (no undelete UI), so "peer has one, local
    doesn't" is sufficient signal to converge."""
    from app.cluster.sync import apply_sync
    from app.models.db import Provider

    # Peer deleted the row at T0; local row's updated_at was later
    # bumped to T0 + 1 hour by background activity (e.g. OAuth refresh).
    deleted_at = datetime.now(timezone.utc) - timedelta(hours=2)
    local_updated_later = deleted_at + timedelta(hours=1)

    async with fresh_db() as db:
        db.add(Provider(
            id="p_bug053_a", name="ghost", provider_type="openai",
            priority=10, enabled=True, extra_config={},
            last_user_edit_at=500.0, updated_at=local_updated_later,
            deleted_at=None,  # local doesn't know it's dead yet
        ))
        await db.commit()

        # Peer payload: same user-edit stamp, OLDER updated_at, but
        # carries the tombstone.
        await apply_sync(db, {"providers": [_provider_payload(
            id="p_bug053_a", name="ghost",
            last_user_edit_at=500.0,
            updated_at=deleted_at,
            deleted_at=deleted_at,
        )]})
        await db.commit()

        local = (await db.execute(
            select(Provider).where(Provider.id == "p_bug053_a")
        )).scalar_one()
        assert local.deleted_at is not None, \
            "BUG-053: peer's tombstone must propagate even when " \
            "local.updated_at is newer than peer.deleted_at"
        assert local.enabled is False, \
            "tombstone propagation must also flip enabled=False"


@pytest.mark.asyncio
async def test_bug053_tombstone_propagates_with_tied_user_edit_at(fresh_db):
    """Exact reproduction of the live 2026-05-20 case: peer + local
    share the same last_user_edit_at (e.g. peer's tombstone was set
    without bumping last_user_edit_at, then both sides did sync
    cycles that converged everything except deleted_at). The legacy
    LWW path's strict-greater check on user_edit_at + the tombstone
    branch's pre-fix gate combined to lock peers out of the
    tombstone entirely."""
    from app.cluster.sync import apply_sync
    from app.models.db import Provider

    deleted_at = datetime.now(timezone.utc) - timedelta(hours=18)
    local_updated = deleted_at + timedelta(hours=12)  # 12h of bg activity

    async with fresh_db() as db:
        db.add(Provider(
            id="p_bug053_b", name="skew-from-new", provider_type="openai",
            priority=10, enabled=False,  # admin had already disabled
            extra_config={},
            last_user_edit_at=1779248020.721197,
            updated_at=local_updated,
            deleted_at=None,
        ))
        await db.commit()

        await apply_sync(db, {"providers": [_provider_payload(
            id="p_bug053_b", name="skew-from-new",
            enabled=False,
            last_user_edit_at=1779248020.721197,  # tied
            updated_at=deleted_at,                # peer's older
            deleted_at=deleted_at,
        )]})
        await db.commit()

        local = (await db.execute(
            select(Provider).where(Provider.id == "p_bug053_b")
        )).scalar_one()
        assert local.deleted_at is not None, \
            "BUG-053: tied user-edit + older peer updated_at must " \
            "still propagate the tombstone"


@pytest.mark.asyncio
async def test_bug053_local_tombstone_not_overwritten_by_peer_tombstone(fresh_db):
    """Symmetric guard: when both sides already have a tombstone, the
    fix must NOT clobber local's deleted_at with peer's. The
    pre-existing 'local tombstone wins' branch (line 189-192) handles
    this — the new short-circuit must defer to it. Without this guard,
    the fix could regress into "any peer tombstone overwrites any
    local tombstone", which would cause the deletion timestamp to
    flap during sync cycles."""
    from app.cluster.sync import apply_sync
    from app.models.db import Provider

    local_deleted = datetime.now(timezone.utc) - timedelta(hours=1)
    peer_deleted_later = local_deleted + timedelta(minutes=30)

    async with fresh_db() as db:
        db.add(Provider(
            id="p_bug053_c", name="dead-twice", provider_type="openai",
            priority=10, enabled=False, extra_config={},
            last_user_edit_at=600.0, updated_at=local_deleted,
            deleted_at=local_deleted,
        ))
        await db.commit()

        await apply_sync(db, {"providers": [_provider_payload(
            id="p_bug053_c", name="dead-twice",
            enabled=False,
            last_user_edit_at=600.0,
            updated_at=peer_deleted_later,
            deleted_at=peer_deleted_later,
        )]})
        await db.commit()

        local = (await db.execute(
            select(Provider).where(Provider.id == "p_bug053_c")
        )).scalar_one()
        # Local tombstone should remain — first-delete-wins semantics.
        # The peer's later deleted_at must not clobber it.
        assert local.deleted_at == local_deleted.replace(tzinfo=None), \
            "local tombstone must not be replaced by a later peer tombstone"
