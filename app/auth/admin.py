"""
Admin session authentication.
Web UI uses cookie-based sessions; API callers use x-api-key.
"""
import hashlib
import secrets
import time
import logging
from typing import Optional
from dataclasses import dataclass

from fastapi import HTTPException, Request, Depends, Cookie
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from passlib.context import CryptContext

from app.models.database import get_db
from app.models.db import User

logger = logging.getLogger(__name__)

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# In-process session store (Redis upgrade path via same interface as CoT sessions)
_sessions: dict[str, dict] = {}
SESSION_TTL_SEC = 86400  # 24 hours


def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


def create_session(user_id: str, username: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "created_at": time.time(),
    }
    return token


def destroy_session(token: str):
    _sessions.pop(token, None)


def _get_session(token: str) -> Optional[dict]:
    s = _sessions.get(token)
    if not s:
        return None
    if time.time() - s["created_at"] > SESSION_TTL_SEC:
        _sessions.pop(token, None)
        return None
    return s


@dataclass
class AdminUser:
    user_id: str
    username: str
    role: str


def _extract_token(request: Request) -> Optional[str]:
    # Cookie (browser sessions)
    token = request.cookies.get("session")
    if token:
        return token
    # Authorization header (API-style admin access)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


async def require_admin(request: Request) -> AdminUser:
    token = _extract_token(request)
    if not token:
        raise HTTPException(401, "Not authenticated")
    s = _get_session(token)
    if not s:
        raise HTTPException(401, "Session expired or invalid")
    if s["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    return AdminUser(user_id=s["user_id"], username=s["username"], role=s["role"])


async def require_any_user(request: Request) -> AdminUser:
    token = _extract_token(request)
    if not token:
        raise HTTPException(401, "Not authenticated")
    s = _get_session(token)
    if not s:
        raise HTTPException(401, "Session expired or invalid")
    return AdminUser(user_id=s["user_id"], username=s["username"], role=s["role"])


async def ensure_default_admin(db: AsyncSession):
    """Create default admin/admin on first boot if no users exist."""
    result = await db.execute(select(User))
    if result.first() is None:
        from app.models.db import User as UserModel
        import secrets as _s
        admin = UserModel(
            id=_s.token_hex(8),
            username="admin",
            password_hash=hash_password("admin"),
            role="admin",
        )
        db.add(admin)
        await db.commit()
        logger.warning("Created default admin user (admin/admin) — change immediately")
