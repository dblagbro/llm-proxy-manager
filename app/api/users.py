"""User management endpoints."""
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.database import get_db
from app.models.db import User
from app.auth.admin import require_admin, AdminUser, hash_password
from app.utils.timefmt import utc_iso

router = APIRouter(prefix="/api/users", tags=["users"])


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"


class UserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None


@router.get("")
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(select(User).order_by(User.created_at))
    return [_serialize(u) for u in result.scalars().all()]


@router.post("")
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.username == body.username))
    if result.scalar_one_or_none():
        raise HTTPException(409, "Username already exists")

    user = User(
        id=secrets.token_hex(8),
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
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
    await db.commit()
    return _serialize(user)


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
):
    user = await _get_or_404(db, user_id)

    # Prevent deleting the last admin
    result = await db.execute(
        select(func.count()).where(User.role == "admin")
    )
    admin_count = result.scalar()
    if user.role == "admin" and admin_count <= 1:
        raise HTTPException(400, "Cannot delete the last admin user")

    await db.delete(user)
    await db.commit()
    return {"ok": True}


async def _get_or_404(db: AsyncSession, user_id: str) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
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
