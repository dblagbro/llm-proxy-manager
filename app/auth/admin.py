"""
Admin session authentication.
Web UI uses cookie-based sessions (persisted to SQLite); API callers use x-api-key.
"""
import secrets
import time
import logging
from typing import Optional
from dataclasses import dataclass

import bcrypt as _bcrypt_lib

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.database import AsyncSessionLocal
from app.models.db import User, Session

logger = logging.getLogger(__name__)

SESSION_TTL_SEC = 86400 * 7   # 7-day rolling sessions
SESSION_IDLE_SEC = 86400      # Extend last_seen on each /me call


def hash_password(plain: str) -> str:
    return _bcrypt_lib.hashpw(plain.encode(), _bcrypt_lib.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt_lib.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


async def create_session(user_id: str, username: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    async with AsyncSessionLocal() as db:
        db.add(Session(
            token=token,
            user_id=user_id,
            username=username,
            role=role,
            created_at=now,
            last_seen_at=now,
        ))
        await db.commit()
    return token


async def destroy_session(token: str):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Session).where(Session.token == token))
        await db.commit()


async def _get_session(token: str) -> Optional[dict]:
    now = time.time()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Session).where(Session.token == token))
        s = result.scalar_one_or_none()
        if not s:
            logger.warning("session_not_found token_prefix=%s", token[:8])
            return None
        # Expire sessions idle > SESSION_TTL_SEC since last seen
        if now - s.last_seen_at > SESSION_TTL_SEC:
            logger.warning("session_expired username=%s idle_sec=%s", s.username, int(now - s.last_seen_at))
            await db.delete(s)
            await db.commit()
            return None
        return {"user_id": s.user_id, "username": s.username, "role": s.role}


async def touch_session(token: str):
    """Update last_seen_at to keep session alive."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Session).where(Session.token == token))
        s = result.scalar_one_or_none()
        if s:
            s.last_seen_at = time.time()
            await db.commit()


async def purge_expired_sessions():
    cutoff = time.time() - SESSION_TTL_SEC
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Session).where(Session.last_seen_at < cutoff))
        await db.commit()


@dataclass
class AdminUser:
    user_id: str
    username: str
    role: str


def _extract_token(request: Request) -> Optional[str]:
    token = request.cookies.get("session")
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


async def require_admin(request: Request) -> AdminUser:
    token = _extract_token(request)
    if not token:
        raise HTTPException(401, "Not authenticated")
    s = await _get_session(token)
    if not s:
        raise HTTPException(401, "Session expired or invalid")
    if s["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    return AdminUser(user_id=s["user_id"], username=s["username"], role=s["role"])


async def require_any_user(request: Request) -> AdminUser:
    token = _extract_token(request)
    if not token:
        raise HTTPException(401, "Not authenticated")
    s = await _get_session(token)
    if not s:
        raise HTTPException(401, "Session expired or invalid")
    return AdminUser(user_id=s["user_id"], username=s["username"], role=s["role"])


async def ensure_default_admin(db: AsyncSession):
    """Create default admin/admin on first boot if no users exist."""
    result = await db.execute(select(User))
    if result.first() is None:
        admin = User(
            id=secrets.token_hex(8),
            username="admin",
            password_hash=hash_password("admin"),
            role="admin",
        )
        db.add(admin)
        await db.commit()
        logger.warning("Created default admin user (admin/admin) — change immediately")
