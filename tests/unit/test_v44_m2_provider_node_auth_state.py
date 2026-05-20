"""v4.4 M-2 — provider_node_auth_state schema + helpers + cluster sync.

Foundation for Path A (per-node grok-bridge) per
``docs/4.4-per-node-bridge-design.md``. The table holds each node's
authoritative view of its OWN bridge's auth state; cluster sync
propagates the full picture so the admin UI on any node + the
routing layer can read the global view.

These tests pin:
- Schema (composite PK, column types).
- ``write_local_state`` upsert + state-transition semantics.
- ``read_state`` / ``read_all_states`` accessors.
- ``is_local_node_routable`` gate logic.
- Cluster sync apply handler (LWW conflict resolution, defensive
  parsing of malformed rows, no overwrite of newer local rows).
- The push payload encodes every column expected by the apply
  handler.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy import select

from app.models.db import Base, Provider, ProviderNodeAuthState


_NODE_ID = "llm-proxy2-www1"
_PROV_ID = "test-grok-web-prov-id"


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession,
                                 expire_on_commit=False)
    async with Session() as s:
        # Seed a Provider row so the FK on auth_state resolves
        s.add(Provider(
            id=_PROV_ID, name="grok-web-test",
            provider_type="grok-web", priority=1,
            enabled=True,
        ))
        await s.commit()
        yield s
    await engine.dispose()


# ── schema ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_composite_primary_key_two_rows_same_provider_different_nodes(db):
    """Different nodes can have rows for the same provider — the
    composite PK is (provider_id, node_id)."""
    db.add(ProviderNodeAuthState(
        provider_id=_PROV_ID, node_id="www1",
        auth_state="ok", last_check_at=datetime.utcnow(),
    ))
    db.add(ProviderNodeAuthState(
        provider_id=_PROV_ID, node_id="www2",
        auth_state="needs_reauth", last_check_at=datetime.utcnow(),
    ))
    await db.commit()
    rs = await db.execute(select(ProviderNodeAuthState))
    rows = rs.scalars().all()
    assert len(rows) == 2
    assert {r.node_id for r in rows} == {"www1", "www2"}


# ── write_local_state ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_local_state_inserts_when_missing(db, monkeypatch):
    from app.routing import node_auth_state
    from app.config import settings
    monkeypatch.setattr(settings, "cluster_node_id", _NODE_ID, raising=False)
    await node_auth_state.write_local_state(db, _PROV_ID, "ok")
    await db.commit()
    row = await node_auth_state.read_state(db, _PROV_ID)
    assert row is not None
    assert row.auth_state == "ok"
    assert row.node_id == _NODE_ID
    assert row.last_ok_at is not None  # auto-set on ok
    assert row.last_check_at is not None


@pytest.mark.asyncio
async def test_write_local_state_updates_existing_row(db, monkeypatch):
    from app.routing import node_auth_state
    from app.config import settings
    monkeypatch.setattr(settings, "cluster_node_id", _NODE_ID, raising=False)
    await node_auth_state.write_local_state(db, _PROV_ID, "ok")
    await db.commit()
    await asyncio.sleep(0.01)  # ensure last_check_at advances
    await node_auth_state.write_local_state(
        db, _PROV_ID, "needs_reauth", last_error="cookies expired"
    )
    await db.commit()
    row = await node_auth_state.read_state(db, _PROV_ID)
    assert row.auth_state == "needs_reauth"
    assert row.last_error == "cookies expired"
    # last_ok_at stays from the previous OK observation
    assert row.last_ok_at is not None


@pytest.mark.asyncio
async def test_write_local_state_rejects_invalid_auth_state(db, monkeypatch):
    from app.routing import node_auth_state
    from app.config import settings
    monkeypatch.setattr(settings, "cluster_node_id", _NODE_ID, raising=False)
    with pytest.raises(ValueError, match="invalid auth_state"):
        await node_auth_state.write_local_state(db, _PROV_ID, "garbage")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_write_local_state_truncates_long_error(db, monkeypatch):
    from app.routing import node_auth_state
    from app.config import settings
    monkeypatch.setattr(settings, "cluster_node_id", _NODE_ID, raising=False)
    huge = "x" * 2000
    await node_auth_state.write_local_state(
        db, _PROV_ID, "needs_reauth", last_error=huge
    )
    await db.commit()
    row = await node_auth_state.read_state(db, _PROV_ID)
    assert len(row.last_error) < len(huge)
    assert "[truncated]" in row.last_error


# ── routing gate ─────────────────────────────────────────────────


def test_is_local_node_routable_ok():
    from app.routing.node_auth_state import is_local_node_routable
    r = ProviderNodeAuthState(provider_id="x", node_id="n",
                               auth_state="ok",
                               last_check_at=datetime.utcnow())
    assert is_local_node_routable(r) is True


def test_is_local_node_routable_not_ok_states():
    from app.routing.node_auth_state import is_local_node_routable
    for state in ("expired", "needs_reauth", "never_authed", "bridge_down"):
        r = ProviderNodeAuthState(provider_id="x", node_id="n",
                                   auth_state=state,
                                   last_check_at=datetime.utcnow())
        assert is_local_node_routable(r) is False, f"{state} should not be routable"


def test_is_local_node_routable_none_means_unknown():
    from app.routing.node_auth_state import is_local_node_routable
    assert is_local_node_routable(None) is False


# ── cluster sync apply (LWW conflict resolution) ──────────────────


@pytest.mark.asyncio
async def test_sync_apply_inserts_new_rows(db):
    from app.cluster.sync_handlers import _apply_provider_node_auth_states
    rows = [{
        "provider_id": _PROV_ID, "node_id": "peer-www2",
        "auth_state": "ok",
        "last_check_at": "2026-05-20T15:00:00",
        "last_ok_at": "2026-05-20T15:00:00",
        "reauth_url": None, "last_error": None,
    }]
    await _apply_provider_node_auth_states(db, rows)
    await db.commit()
    rs = await db.execute(
        select(ProviderNodeAuthState).where(ProviderNodeAuthState.node_id == "peer-www2")
    )
    r = rs.scalar_one()
    assert r.auth_state == "ok"


@pytest.mark.asyncio
async def test_sync_apply_lww_skips_older_incoming(db):
    """If our local row is newer, ignore the peer's older row."""
    from app.cluster.sync_handlers import _apply_provider_node_auth_states
    now = datetime.utcnow()
    db.add(ProviderNodeAuthState(
        provider_id=_PROV_ID, node_id="peer-www2",
        auth_state="ok", last_check_at=now,
    ))
    await db.commit()
    # Incoming row is 1 hour older — should be skipped.
    older = (now - timedelta(hours=1)).isoformat()
    await _apply_provider_node_auth_states(db, [{
        "provider_id": _PROV_ID, "node_id": "peer-www2",
        "auth_state": "needs_reauth",  # different state — would clobber if not LWW
        "last_check_at": older,
    }])
    await db.commit()
    r = await db.execute(
        select(ProviderNodeAuthState).where(ProviderNodeAuthState.node_id == "peer-www2")
    )
    assert r.scalar_one().auth_state == "ok"  # unchanged


@pytest.mark.asyncio
async def test_sync_apply_lww_accepts_newer_incoming(db):
    """If the peer's row is newer, accept its state."""
    from app.cluster.sync_handlers import _apply_provider_node_auth_states
    now = datetime.utcnow()
    db.add(ProviderNodeAuthState(
        provider_id=_PROV_ID, node_id="peer-www2",
        auth_state="ok", last_check_at=now - timedelta(hours=1),
    ))
    await db.commit()
    await _apply_provider_node_auth_states(db, [{
        "provider_id": _PROV_ID, "node_id": "peer-www2",
        "auth_state": "needs_reauth",
        "last_check_at": now.isoformat(),
    }])
    await db.commit()
    r = await db.execute(
        select(ProviderNodeAuthState).where(ProviderNodeAuthState.node_id == "peer-www2")
    )
    assert r.scalar_one().auth_state == "needs_reauth"


@pytest.mark.asyncio
async def test_sync_apply_ignores_rows_missing_keys(db):
    """Malformed rows (no provider_id or no node_id) get silently
    skipped — defensive vs a buggy peer."""
    from app.cluster.sync_handlers import _apply_provider_node_auth_states
    await _apply_provider_node_auth_states(db, [
        {"node_id": "x"},                # missing provider_id
        {"provider_id": _PROV_ID},       # missing node_id
        {},                               # both missing
    ])
    await db.commit()
    rs = await db.execute(select(ProviderNodeAuthState))
    assert rs.scalars().all() == []


# ── push payload completeness ────────────────────────────────────


def test_push_payload_includes_provider_node_auth_states():
    """Regression guard: the manager.py push_sync() must include the
    new key in the payload dict."""
    from pathlib import Path
    src = Path("app/cluster/manager.py").read_text()
    assert "provider_node_auth_states" in src
    assert "ProviderNodeAuthState" in src
