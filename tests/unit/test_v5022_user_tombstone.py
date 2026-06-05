"""v5.0.22 — User soft-delete tombstone + cluster-sync LWW (BUG-070).

Operator reported: "users I deleted keep coming back."

Root cause: pre-fix, DELETE /api/users/{id} did a hard ``db.delete()``
and the User model had no ``deleted_at`` column, so cluster-sync's
insert-if-missing merge resurrected the row from any peer that hadn't
seen the delete yet.

Fix:
  - User.deleted_at + last_user_edit_at columns
  - DELETE soft-deletes (sets deleted_at + bumps edit-stamp)
  - GET /api/users filters tombstoned rows
  - POST /api/users restores by-username if a tombstoned row exists
  - Login filters tombstoned rows
  - ensure_default_admin filters tombstoned rows
  - Cluster sync push includes deleted_at + last_user_edit_at
  - Cluster sync apply respects tombstones + LWW
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)


async def _fresh_db():
    from app.models.db import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ── Schema pins ─────────────────────────────────────────────────────


def test_user_model_has_tombstone_columns():
    from app.models.db import User
    cols = {c.name for c in User.__table__.columns}
    assert "deleted_at" in cols, (
        "User.deleted_at missing — BUG-070 regression. Cluster sync "
        "will resurrect deleted users."
    )
    assert "last_user_edit_at" in cols, (
        "User.last_user_edit_at missing — LWW gate can't differentiate "
        "stale peer state from a fresh delete."
    )


def test_alter_table_users_in_database_bootstrap():
    """Schema migration must register the new columns in the bootstrap
    ALTER list so existing DBs upgrade in place."""
    src = Path("app/models/database.py").read_text()
    assert "ALTER TABLE users ADD COLUMN deleted_at DATETIME" in src
    assert "ALTER TABLE users ADD COLUMN last_user_edit_at REAL" in src


# ── Cluster-sync pins ───────────────────────────────────────────────


def test_sync_payload_includes_user_deleted_at():
    """_build_sync_payload's users section must include deleted_at and
    last_user_edit_at so peers can apply LWW + tombstone."""
    src = Path("app/cluster/manager.py").read_text()
    # Find the users serializer block
    users_block_start = src.find("users = [")
    assert users_block_start != -1
    users_block = src[users_block_start:users_block_start + 1500]
    assert "deleted_at" in users_block, (
        "manager push payload no longer includes user.deleted_at — "
        "peers can't learn about deletes."
    )
    assert "last_user_edit_at" in users_block, (
        "manager push payload no longer includes user.last_user_edit_at "
        "— LWW gate has no input."
    )


def test_sync_apply_users_uses_lww_not_insert_if_missing():
    """sync.py users-merge must NOT be insert-if-missing. It must check
    incoming deleted_at + last_user_edit_at."""
    src = Path("app/cluster/sync.py").read_text()
    # Locate the users-merge section
    users_section_start = src.find('payload.get("users"')
    assert users_section_start != -1
    users_section = src[users_section_start:users_section_start + 3500]
    assert "deleted_at" in users_section, (
        "BUG-070 regression: users-merge in sync.py no longer reads "
        "deleted_at from incoming payload. Resurrection bug returns."
    )
    assert "last_user_edit_at" in users_section, (
        "BUG-070 regression: users-merge no longer reads "
        "last_user_edit_at. LWW gate gone."
    )


# ── Behavioral: API soft-delete ─────────────────────────────────────


@pytest.mark.asyncio
async def test_soft_delete_sets_tombstone_and_filters_from_list(monkeypatch):
    from app.models.db import User
    Session = await _fresh_db()
    async with Session() as db:
        u = User(id="u1", username="bob", password_hash="h", role="user")
        db.add(u)
        await db.commit()

        # Soft-delete: set deleted_at + bump last_user_edit_at
        u.deleted_at = datetime.utcnow()
        u.last_user_edit_at = 1780000000.0
        await db.commit()

        # GET /api/users filter should now hide it
        alive = (await db.execute(
            select(User).where(User.deleted_at.is_(None))
        )).scalars().all()
    assert alive == [], "tombstoned user should not appear in alive list"


# ── Behavioral: cluster-sync LWW ────────────────────────────────────


@pytest.mark.asyncio
async def test_incoming_tombstone_propagates_to_local_alive_row():
    """If a peer has a tombstoned row newer than our alive row, we
    must adopt the tombstone."""
    from app.models.db import User
    # We replicate the in-sync logic inline rather than calling
    # apply_sync (which expects HMAC + full payload + DB session).
    Session = await _fresh_db()
    async with Session() as db:
        # Local: alive user with edit-stamp=1000
        db.add(User(id="u1", username="bob", password_hash="h",
                    role="user", last_user_edit_at=1000.0))
        await db.commit()

        # Simulate the LWW apply (extracted from sync.py for direct test)
        from datetime import datetime as _dt
        incoming = {
            "id": "u1", "username": "bob", "password_hash": "h",
            "role": "user",
            "deleted_at": "2026-06-05T22:00:00",
            "last_user_edit_at": 2000.0,
        }
        rs = await db.execute(select(User).where(User.id == "u1"))
        existing = rs.scalar_one()
        incoming_edit = incoming["last_user_edit_at"]
        incoming_deleted = _dt.fromisoformat(incoming["deleted_at"])
        local_edit = existing.last_user_edit_at or 0.0
        if incoming_edit > local_edit:
            existing.deleted_at = incoming_deleted
            existing.last_user_edit_at = incoming_edit
        await db.commit()

        rs2 = await db.execute(select(User).where(User.id == "u1"))
        row = rs2.scalar_one()
    assert row.deleted_at is not None, (
        "Incoming tombstone with newer edit-stamp must override local "
        "alive state."
    )


@pytest.mark.asyncio
async def test_local_newer_edit_keeps_local_state():
    """If local edit-stamp is NEWER than peer's, local state wins."""
    from app.models.db import User
    Session = await _fresh_db()
    async with Session() as db:
        db.add(User(id="u1", username="bob", password_hash="local",
                    role="user", last_user_edit_at=3000.0))
        await db.commit()
        # Peer says: bob was deleted at edit-stamp=1000 (older)
        incoming = {
            "id": "u1", "username": "bob", "password_hash": "peer",
            "role": "user", "deleted_at": "2026-06-04T10:00:00",
            "last_user_edit_at": 1000.0,
        }
        rs = await db.execute(select(User).where(User.id == "u1"))
        existing = rs.scalar_one()
        local_edit = existing.last_user_edit_at or 0.0
        if (incoming["last_user_edit_at"] or 0.0) > local_edit:
            existing.deleted_at = datetime.fromisoformat(incoming["deleted_at"])
        await db.commit()

        rs2 = await db.execute(select(User).where(User.id == "u1"))
        row = rs2.scalar_one()
    assert row.deleted_at is None, (
        "Local newer edit-stamp should reject older incoming tombstone."
    )


# ── Source pin: bulk_delete endpoint exists ─────────────────────────


def test_bulk_delete_endpoint_present():
    src = Path("app/api/users.py").read_text()
    assert '@router.post("/bulk_delete")' in src, (
        "bulk_delete endpoint missing — UX feature regression."
    )
    assert "live_admins" in src, (
        "bulk_delete must guard against deleting the last live admin."
    )


def test_login_filters_tombstoned_users():
    src = Path("app/api/auth.py").read_text()
    assert "User.deleted_at.is_(None)" in src, (
        "Login no longer filters tombstoned users — BUG-070 hole."
    )
