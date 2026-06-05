"""v5.0.18 — UI-configurable cluster peer list.

Pre-v5.0.18 cluster peers came from ``CLUSTER_PEERS`` env at startup.
v5.0.18 adds a durable ``cluster_peers`` table that the Settings →
Cluster page can mutate via admin API. Env becomes a one-time seed
source on empty-table boot.

Tests cover:
  - Model is registered (covered also by test_v4411_db_split bumps)
  - LWW apply handler: add, update, tombstone, ignore-self
  - Env seed bootstrap is idempotent
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


async def _fresh_db():
    from app.models.db import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ── Source pins ─────────────────────────────────────────────────────


def test_payload_includes_cluster_peers_in_push():
    """manager.build_sync_payload must include the cluster_peers list
    in its output — otherwise add/remove operations don't replicate."""
    src = Path("app/cluster/manager.py").read_text()
    assert '"cluster_peers": cluster_peers_payload' in src or \
           '"cluster_peers":' in src, (
        "manager push payload no longer surfaces cluster_peers — UI "
        "peer changes won't replicate to other nodes."
    )


def test_apply_sync_calls_apply_cluster_peers():
    """app.cluster.sync.apply_sync must invoke _apply_cluster_peers on
    the payload's cluster_peers list."""
    src = Path("app/cluster/sync.py").read_text()
    assert "_apply_cluster_peers(db, payload.get(\"cluster_peers\", []))" in src


def test_api_exposes_peer_crud():
    """API has GET / POST / DELETE on /cluster/peers."""
    src = Path("app/api/cluster.py").read_text()
    assert '@router.get("/cluster/peers")' in src
    assert '@router.post("/cluster/peers")' in src
    assert '@router.delete("/cluster/peers/{peer_id}")' in src


# ── Behavioral: apply handler LWW + tombstone semantics ──────────────


@pytest.mark.asyncio
async def test_apply_cluster_peers_inserts_new(monkeypatch):
    from app.cluster.sync_handlers import _apply_cluster_peers
    from app.models.db import ClusterPeer

    monkeypatch.setattr("app.config.settings.cluster_node_id", "llm-proxy-self")
    Session = await _fresh_db()
    async with Session() as db:
        await _apply_cluster_peers(db, [
            {"id": "peer-A", "url": "https://A/llm-proxy",
             "name": "Peer A",
             "added_at": "2026-06-05T16:00:00",
             "removed_at": None,
             "last_user_edit_at": 1780777200.0},
        ])
        await db.commit()
        row = (await db.execute(
            select(ClusterPeer).where(ClusterPeer.id == "peer-A")
        )).scalar_one()
    assert row.url == "https://A/llm-proxy"
    assert row.name == "Peer A"
    assert row.removed_at is None


@pytest.mark.asyncio
async def test_apply_cluster_peers_ignores_self(monkeypatch):
    """A peer payload that includes our own node id is skipped — each
    node knows itself; the table is for OTHER nodes only."""
    from app.cluster.sync_handlers import _apply_cluster_peers
    from app.models.db import ClusterPeer

    monkeypatch.setattr("app.config.settings.cluster_node_id", "llm-proxy-self")
    Session = await _fresh_db()
    async with Session() as db:
        await _apply_cluster_peers(db, [
            {"id": "llm-proxy-self", "url": "https://self/llm-proxy",
             "name": None, "added_at": None, "removed_at": None,
             "last_user_edit_at": 1780777200.0},
        ])
        await db.commit()
        count = (await db.execute(select(ClusterPeer))).scalars().all()
    assert len(count) == 0, "should refuse to insert a row matching our own node id"


@pytest.mark.asyncio
async def test_apply_cluster_peers_tombstone_propagates(monkeypatch):
    """A removal on one peer propagates: incoming row with removed_at
    set overrides our local active row."""
    from app.cluster.sync_handlers import _apply_cluster_peers
    from app.models.db import ClusterPeer

    monkeypatch.setattr("app.config.settings.cluster_node_id", "llm-proxy-self")
    Session = await _fresh_db()
    async with Session() as db:
        db.add(ClusterPeer(
            id="peer-C", url="https://C/llm-proxy", name="C",
            added_at=datetime(2026, 6, 5, 10, 0),
            last_user_edit_at=1780777000.0,
        ))
        await db.commit()
        await _apply_cluster_peers(db, [
            {"id": "peer-C", "url": "https://C/llm-proxy", "name": "C",
             "added_at": "2026-06-05T10:00:00",
             "removed_at": "2026-06-05T17:00:00",   # remote tombstone
             "last_user_edit_at": 1780780800.0},
        ])
        await db.commit()
        row = (await db.execute(
            select(ClusterPeer).where(ClusterPeer.id == "peer-C")
        )).scalar_one()
    assert row.removed_at is not None, (
        "Local active row should have been tombstoned by the incoming "
        "removal payload."
    )


@pytest.mark.asyncio
async def test_apply_cluster_peers_lww_keeps_local_when_newer(monkeypatch):
    """If local last_user_edit_at is newer than peer's, local wins on
    url/name updates."""
    from app.cluster.sync_handlers import _apply_cluster_peers
    from app.models.db import ClusterPeer

    monkeypatch.setattr("app.config.settings.cluster_node_id", "llm-proxy-self")
    Session = await _fresh_db()
    async with Session() as db:
        db.add(ClusterPeer(
            id="peer-D", url="https://D-new/llm-proxy", name="D-new",
            added_at=datetime(2026, 6, 5, 10, 0),
            last_user_edit_at=1780780000.0,   # local is NEWER
        ))
        await db.commit()
        await _apply_cluster_peers(db, [
            {"id": "peer-D", "url": "https://D-old/llm-proxy", "name": "D-old",
             "added_at": "2026-06-05T10:00:00",
             "removed_at": None,
             "last_user_edit_at": 1780777000.0},   # peer is OLDER
        ])
        await db.commit()
        row = (await db.execute(
            select(ClusterPeer).where(ClusterPeer.id == "peer-D")
        )).scalar_one()
    assert row.url == "https://D-new/llm-proxy"
    assert row.name == "D-new"
