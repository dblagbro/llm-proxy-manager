"""User management endpoints."""
import secrets
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.database import get_db
from app.models.db import User
from app.auth.admin import require_admin, AdminUser, hash_password
from app.utils.timefmt import utc_iso

router = APIRouter(prefix="/api/users", tags=["users"])


class UserCreate(BaseModel):
    # BUG-042 fix: reject empty username + short password at the boundary.
    # UserUpdate (PATCH) intentionally treats falsy password as "no change"
    # (see update_user below — `if body.password:`); creation has no such
    # semantic — a brand-new account must have a real credential.
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=8)
    role: str = "user"


class UserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None


class BulkDeleteBody(BaseModel):
    # v5.0.22 — bulk delete endpoint. UI is the primary caller (Select
    # All + Delete N selected). Server applies the same last-admin
    # guard + soft-delete semantics as single delete; partial success
    # is supported (some ids may 404 or be the last admin).
    ids: list[str] = Field(..., min_length=1, max_length=500)


@router.get("")
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    # v5.0.22 — filter tombstoned rows (BUG-070). Pre-fix the API
    # returned soft-deleted users; UI showed them as still alive.
    result = await db.execute(
        select(User).where(User.deleted_at.is_(None)).order_by(User.created_at)
    )
    return [_serialize(u) for u in result.scalars().all()]


@router.post("")
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    # v5.0.22 — if a tombstoned user with the same username exists,
    # restore it (clears deleted_at, updates password/role) rather
    # than colliding on the unique constraint.
    result = await db.execute(select(User).where(User.username == body.username))
    existing = result.scalar_one_or_none()
    if existing is not None:
        if existing.deleted_at is None:
            raise HTTPException(409, "Username already exists")
        existing.deleted_at = None
        existing.password_hash = hash_password(body.password)
        existing.role = body.role
        existing.last_user_edit_at = time.time()
        await db.commit()
        await db.refresh(existing)
        return _serialize(existing)

    user = User(
        id=secrets.token_hex(8),
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        last_user_edit_at=time.time(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _serialize(user)


@router.patch("/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdate,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    user = await _get_or_404(db, user_id)
    if body.password:
        user.password_hash = hash_password(body.password)
    if body.role:
        user.role = body.role
    user.last_user_edit_at = time.time()
    await db.commit()
    return _serialize(user)


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    user = await _get_or_404(db, user_id)

    # Prevent deleting the last admin (only count live admins)
    result = await db.execute(
        select(func.count()).where(
            User.role == "admin",
            User.deleted_at.is_(None),
        )
    )
    admin_count = result.scalar()
    if user.role == "admin" and admin_count <= 1:
        raise HTTPException(400, "Cannot delete the last admin user")

    # v5.0.22 — SOFT delete (BUG-070). Pre-fix this was a hard
    # db.delete() and cluster sync's insert-if-missing merge brought
    # the row back from peers that hadn't seen the delete yet. Now
    # we set deleted_at + bump last_user_edit_at; cluster sync
    # propagates the tombstone via LWW.
    user.deleted_at = datetime.utcnow()
    user.last_user_edit_at = time.time()
    await db.commit()
    return {"ok": True}


@router.post("/bulk_delete")
async def bulk_delete_users(
    body: BulkDeleteBody,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    """v5.0.22 — soft-delete many users in one round-trip.

    Returns ``{"deleted": [...ids], "errors": [{"id": …, "reason": …}]}``.
    Partial success is normal: an id that doesn't exist gets a
    not_found error; an id that would leave zero live admins gets a
    last_admin error. Self-delete is rejected per row.
    """
    # Count current live admins ONCE; decrement as we tombstone admins
    # so we never drop below 1.
    result = await db.execute(
        select(func.count()).where(
            User.role == "admin",
            User.deleted_at.is_(None),
        )
    )
    live_admins = result.scalar() or 0

    deleted, errors = [], []
    now_dt = datetime.utcnow()
    now_ts = time.time()
    for uid in body.ids:
        u = (await db.execute(
            select(User).where(User.id == uid, User.deleted_at.is_(None))
        )).scalar_one_or_none()
        if u is None:
            errors.append({"id": uid, "reason": "not_found"})
            continue
        if u.username == admin.username:
            errors.append({"id": uid, "reason": "cannot_delete_self"})
            continue
        if u.role == "admin" and live_admins <= 1:
            errors.append({"id": uid, "reason": "last_admin"})
            continue
        u.deleted_at = now_dt
        u.last_user_edit_at = now_ts
        if u.role == "admin":
            live_admins -= 1
        deleted.append(uid)
    await db.commit()
    return {"deleted": deleted, "errors": errors}


async def _get_or_404(db: AsyncSession, user_id: str) -> User:
    # v5.0.22 — refuse to look up tombstoned rows. PATCH / DELETE on
    # an already-deleted user should 404, not silently mutate a
    # soft-deleted row.
    result = await db.execute(
        select(User).where(User.id == user_id, User.deleted_at.is_(None))
    )
    u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(404, "User not found")
    return u


def _serialize(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "created_at": utc_iso(u.created_at),
    }
