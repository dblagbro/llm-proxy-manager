"""Admin login/logout endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Response, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import get_db
from app.models.db import User
from app.auth.admin import verify_password, create_session, destroy_session, require_any_user, AdminUser

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

    token = create_session(user.id, user.username, user.role)
    response.set_cookie(
        "session", token,
        httponly=True, samesite="lax", max_age=86400,
    )
    return {"username": user.username, "role": user.role}


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        destroy_session(token)
    response.delete_cookie("session")
    return {"ok": True}


@router.get("/me")
async def me(admin: AdminUser = Depends(require_any_user)):
    return {"username": admin.username, "role": admin.role}
