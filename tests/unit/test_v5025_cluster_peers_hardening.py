"""v5.0.25 / remediation Batch 4 — cluster_peers hardening.

Covers BUG-057, BUG-061, BUG-062, BUG-063, BUG-064.

  - BUG-057: prune-on-startup deletes any cluster_peers row whose id
    matches the current cluster_node_id.
  - BUG-061: ``_reload_peers_from_db`` swap runs under ``_peers_lock``.
  - BUG-062: POST /cluster/peers rejects http:// URLs unless DEBUG.
  - BUG-063: ClusterPage uses ConfirmDialog (not browser confirm()).
  - BUG-064: ``_parse_iso_keep_naive`` returns None on unrecognized
    types instead of returning the raw value (would crash the next
    LWW comparison).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)


# ── Source pins ─────────────────────────────────────────────────────


def test_prune_self_row_helper_exists():
    from app.cluster.manager import _prune_self_row_from_db
    assert callable(_prune_self_row_from_db)


def test_bootstrap_calls_prune_before_seed():
    """The startup _bootstrap closure must call prune before seed +
    reload so a renamed cluster_node_id doesn't carry an orphan."""
    src = Path("app/cluster/manager.py").read_text()
    boot = src.find("async def _bootstrap")
    assert boot != -1
    body = src[boot:boot + 1500]
    prune_idx = body.find("_prune_self_row_from_db")
    seed_idx = body.find("_seed_peers_from_env_if_empty")
    reload_idx = body.find("_reload_peers_from_db")
    assert prune_idx != -1 and seed_idx != -1 and reload_idx != -1
    assert prune_idx < seed_idx < reload_idx, (
        "_bootstrap must call: prune → seed → reload (in that order)."
    )


def test_reload_peers_holds_lock():
    """_reload_peers_from_db must wrap the in-memory swap in
    _peers_lock so concurrent readers see consistent state."""
    src = Path("app/cluster/manager.py").read_text()
    assert "_peers_lock = asyncio.Lock()" in src
    fn_start = src.find("async def _reload_peers_from_db")
    next_def = src.find("async def ", fn_start + 1)
    body = src[fn_start:next_def if next_def != -1 else fn_start + 3000]
    assert "async with _peers_lock:" in body, (
        "BUG-061 regression: _reload_peers_from_db no longer "
        "acquires _peers_lock around the dict swap."
    )


def test_cluster_peers_post_enforces_https():
    src = Path("app/api/cluster.py").read_text()
    assert "must use https://" in src, (
        "BUG-062 regression: POST /cluster/peers no longer enforces "
        "https:// (cluster sync carries credentials)."
    )


def test_parse_iso_keep_naive_handles_bad_types():
    """BUG-064: passing a non-datetime / non-string value must return
    None and log, not return the raw value."""
    from app.cluster.sync_handlers import _parse_iso_keep_naive
    assert _parse_iso_keep_naive(None) is None
    assert _parse_iso_keep_naive("") is None
    assert _parse_iso_keep_naive(1780777200) is None  # int timestamp
    assert _parse_iso_keep_naive(1780777200.5) is None  # float timestamp
    assert _parse_iso_keep_naive({"bad": "shape"}) is None
    assert _parse_iso_keep_naive(["list"]) is None
    # Still works on real values
    assert isinstance(_parse_iso_keep_naive("2026-06-05T12:00:00"), datetime)
    assert isinstance(_parse_iso_keep_naive(datetime(2026, 6, 5)), datetime)


def test_clusterpage_uses_confirm_dialog_not_browser_confirm():
    src = Path("frontend/src/pages/ClusterPage.tsx").read_text()
    assert "ConfirmDialog" in src, "ClusterPage must import ConfirmDialog"
    assert "confirm(`" not in src, (
        "BUG-063 regression: ClusterPage reverted to browser confirm() "
        "instead of using ConfirmDialog component (Playwright-unfriendly)."
    )


# ── Behavioral: prune-on-startup ────────────────────────────────────


async def _fresh_db():
    from app.models.db import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_prune_self_row_removes_orphan(monkeypatch):
    """If a row exists in cluster_peers whose id matches our current
    cluster_node_id, _prune_self_row_from_db must hard-delete it.
    Simulates the rename case: operator changed CLUSTER_NODE_ID and
    restarted; the OLD node id row is now indistinguishable from a
    peer's own row.
    """
    from app.models.db import ClusterPeer
    from app.cluster.manager import _prune_self_row_from_db

    monkeypatch.setattr(
        "app.config.settings.cluster_node_id", "llm-proxy-self"
    )
    Session = await _fresh_db()
    async with Session() as db:
        db.add(ClusterPeer(
            id="llm-proxy-self",
            url="https://self/llm-proxy",
            name="me",
            added_at=datetime(2026, 6, 5, 10),
            last_user_edit_at=1780000000.0,
        ))
        db.add(ClusterPeer(
            id="llm-proxy-other",
            url="https://other/llm-proxy",
            name="other",
            added_at=datetime(2026, 6, 5, 10),
            last_user_edit_at=1780000000.0,
        ))
        await db.commit()

    # Invoke prune; should remove the self row but NOT the other.
    n = await _prune_self_row_from_db(lambda: Session())
    assert n == 1

    async with Session() as db:
        rs = await db.execute(select(ClusterPeer))
        rows = rs.scalars().all()
        ids = {r.id for r in rows}
    assert ids == {"llm-proxy-other"}, (
        f"Expected only 'llm-proxy-other' to survive prune; got {ids}"
    )


@pytest.mark.asyncio
async def test_prune_self_row_is_noop_when_no_self_row():
    """When cluster_peers has no row matching current node id, the
    prune helper is a clean no-op (returns 0)."""
    from app.models.db import ClusterPeer
    from app.cluster.manager import _prune_self_row_from_db

    Session = await _fresh_db()
    async with Session() as db:
        db.add(ClusterPeer(
            id="peer-a", url="https://a/llm-proxy",
            added_at=datetime(2026, 6, 5),
            last_user_edit_at=1780000000.0,
        ))
        await db.commit()

    # cluster_node_id default is empty / something not in the table.
    n = await _prune_self_row_from_db(lambda: Session())
    assert n == 0
