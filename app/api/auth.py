"""Admin login/logout endpoints."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import get_db
from app.models.db import User
from app.auth.admin import (
    verify_password, create_session, destroy_session, touch_session,
    require_any_user, AdminUser, _extract_token,
    SESSION_COOKIE_NAME, SESSION_COOKIE_PATH,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_COOKIE_MAX_AGE = 86400 * 7  # 7 days, matches SESSION_TTL_SEC

# Legacy cookie name/path used before v2.6.1 — deleted on login/logout so the
# old cookie at path=/ doesn't keep overwriting the correctly-scoped one.
_LEGACY_COOKIE_NAME = "session"
_LEGACY_COOKIE_PATH = "/"


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")

    token = await create_session(user.id, user.username, user.role)
    # v2.6.1 bugfix: scoped path + unique name — otherwise other apps on
    # voipguru.org that set a cookie named `session` at path=/ overwrite
    # ours, which was the "logged out every minute" bug.
    response.set_cookie(
        SESSION_COOKIE_NAME, token,
        httponly=True, samesite="lax", secure=True, max_age=SESSION_COOKIE_MAX_AGE,
        path=SESSION_COOKIE_PATH,
    )
    # Kill any lingering legacy cookie at path=/ that could still shadow us.
    response.delete_cookie(_LEGACY_COOKIE_NAME, path=_LEGACY_COOKIE_PATH)
    return {"username": user.username, "role": user.role}


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = (
        request.cookies.get(SESSION_COOKIE_NAME)
        or request.cookies.get(_LEGACY_COOKIE_NAME)
    )
    if token:
        await destroy_session(token)
    response.delete_cookie(SESSION_COOKIE_NAME, path=SESSION_COOKIE_PATH)
    response.delete_cookie(_LEGACY_COOKIE_NAME, path=_LEGACY_COOKIE_PATH)
    return {"ok": True}


@router.get("/me")
async def me(request: Request, admin: AdminUser = Depends(require_any_user),
             db: AsyncSession = Depends(get_db)):
    token = _extract_token(request)
    if token:
        await touch_session(token)
    # v3.0 R1: include per-user display preferences (timezone, time_format)
    res = await db.execute(select(User).where(User.username == admin.username))
    user = res.scalar_one_or_none()
    return {
        "username": admin.username,
        "role": admin.role,
        "timezone": getattr(user, "timezone", None) if user else None,
        "time_format": getattr(user, "time_format", None) if user else None,
    }


class PreferencesUpdate(BaseModel):
    timezone: Optional[str] = None     # IANA name, or empty string to clear
    time_format: Optional[str] = None  # '12h' | '24h' | empty string to clear


_VALID_TIME_FORMATS = {"12h", "24h", ""}


@router.patch("/preferences")
async def update_preferences(
    body: PreferencesUpdate,
    admin: AdminUser = Depends(require_any_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the logged-in user's display preferences.

    Self-service: no admin role required (any authenticated user can edit
    their own prefs). NULL on either field means "follow browser locale";
    empty-string in the payload is the way to clear back to NULL.
    """
    if body.time_format is not None and body.time_format not in _VALID_TIME_FORMATS:
        raise HTTPException(400, "time_format must be '12h', '24h', or empty string")
    res = await db.execute(select(User).where(User.username == admin.username))
    user = res.scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "user not found")
    if body.timezone is not None:
        user.timezone = body.timezone or None
    if body.time_format is not None:
        user.time_format = body.time_format or None
    await db.commit()
    return {
        "username": user.username,
        "timezone": user.timezone,
        "time_format": user.time_format,
    }
