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
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


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
    response.set_cookie(
        "session", token,
        httponly=True, samesite="lax", secure=True, max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
    )
    return {"username": user.username, "role": user.role}


SESSION_COOKIE_MAX_AGE = 86400 * 7  # 7 days, matches SESSION_TTL_SEC


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        await destroy_session(token)
    response.delete_cookie("session", path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request, admin: AdminUser = Depends(require_any_user)):
    token = _extract_token(request)
    if token:
        await touch_session(token)
    return {"username": admin.username, "role": admin.role}
