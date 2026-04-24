"""Admin login/logout endpoints."""
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
async def me(request: Request, admin: AdminUser = Depends(require_any_user)):
    token = _extract_token(request)
    if token:
        await touch_session(token)
    return {"username": admin.username, "role": admin.role}
