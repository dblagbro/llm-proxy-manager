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
    require_any_user, AdminUser, _extract_token, _get_session,
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
    # v5.0.22 — login must refuse tombstoned users (BUG-070). Pre-fix
    # a deleted user could still authenticate as long as some peer
    # had resurrected them via insert-if-missing cluster sync.
    result = await db.execute(
        select(User).where(
            User.username == body.username,
            User.deleted_at.is_(None),
        )
    )
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


@router.get("/session")
async def session_probe(request: Request, db: AsyncSession = Depends(get_db)):
    """Unauthenticated-safe session probe — always returns 200. The frontend
    boot check calls this instead of /me so a logged-out page load does not
    log a 401 "failed to load resource" console error (BUG-020). /me keeps
    its 401 contract for authenticated callers."""
    token = _extract_token(request)
    sess = await _get_session(token) if token else None
    if not sess:
        return {"authenticated": False}
    await touch_session(token)
    res = await db.execute(select(User).where(User.username == sess["username"]))
    user = res.scalar_one_or_none()
    return {
        "authenticated": True,
        "username": sess["username"],
        "role": sess["role"],
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


# ── v5.22.7 — self-service password reset (option B) ────────────────────────
#
# Threat model notes, because this is the only UNAUTHENTICATED write endpoint
# on the service:
#   * Anti-enumeration — /request always returns the same 200 body whether or
#     not the account exists, has an email, or the mail send succeeded.
#   * Tokens are 256-bit url-safe randoms; only their SHA-256 is stored, so a
#     database read cannot be replayed into a takeover.
#   * Single-use + short TTL, and any password change elsewhere spends them.
#   * Per-IP and per-account rate limits, so this cannot be used as a mail
#     cannon or to grind usernames.
#   * Timing is not constant, but the work done is identical in both branches
#     up to the point where a row is or is not found.

RESET_TTL_MINUTES = 30
_RESET_MAX_PER_IP_PER_HOUR = 10
_RESET_MAX_PER_USER_PER_HOUR = 3

# in-process counters: {bucket_key: [timestamps]}
_reset_attempts: dict[str, list[float]] = {}


def _reset_rate_ok(key: str, limit: int, window_sec: float = 3600.0) -> bool:
    """Sliding-window limiter. Returns False when `key` is over `limit`."""
    import time as _t
    now = _t.time()
    hits = [t for t in _reset_attempts.get(key, []) if now - t < window_sec]
    if len(hits) >= limit:
        _reset_attempts[key] = hits
        return False
    hits.append(now)
    _reset_attempts[key] = hits
    if len(_reset_attempts) > 5000:          # crude bound; this is best-effort
        for k in [k for k, v in _reset_attempts.items() if not v or now - max(v) > window_sec]:
            _reset_attempts.pop(k, None)
    return True


def _hash_reset_token(raw: str) -> str:
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PasswordResetRequest(BaseModel):
    # Accepts either the username or the email address — people type both.
    identifier: str


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str


# Identical response for every outcome. Do not make this specific.
_RESET_GENERIC = {
    "ok": True,
    "message": ("If that account exists and has an email address on file, "
                "a reset link has been sent."),
}


@router.post("/password-reset/request")
async def password_reset_request(
    body: PasswordResetRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    import logging
    import secrets as _secrets
    import time as _t

    log = logging.getLogger(__name__)
    ident = (body.identifier or "").strip()
    client_ip = (request.client.host if request.client else "?") or "?"

    if not ident or not _reset_rate_ok(f"ip:{client_ip}", _RESET_MAX_PER_IP_PER_HOUR):
        log.info("password_reset.request throttled_or_empty ip=%s", client_ip)
        return _RESET_GENERIC

    row = await db.execute(
        select(User).where(
            (User.username == ident) | (User.email == ident),
            User.deleted_at.is_(None),
        )
    )
    user = row.scalars().first()

    if user is None or not user.email:
        # Same response as success — never reveal which of these it was.
        log.info("password_reset.request no_target ip=%s", client_ip)
        return _RESET_GENERIC

    if not _reset_rate_ok(f"user:{user.id}", _RESET_MAX_PER_USER_PER_HOUR):
        log.info("password_reset.request user_throttled user=%s", user.id)
        return _RESET_GENERIC

    from app.models.db_user import PasswordResetToken

    raw_token = _secrets.token_urlsafe(32)
    now = _t.time()
    db.add(PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_reset_token(raw_token),
        created_at=now,
        expires_at=now + RESET_TTL_MINUTES * 60,
        requested_ip=client_ip,
    ))
    await db.commit()

    # Build the link against the public base path (sub-path deploy aware).
    base = str(request.base_url).rstrip("/")
    root = request.headers.get("x-forwarded-prefix") or "/llm-proxy2"
    reset_url = f"{base}{root}/reset-password?token={raw_token}"

    from app.utils.mailer import render_password_reset_email, send_email_async
    sent = await send_email_async(
        user.email,
        "llm-proxy — password reset",
        render_password_reset_email(reset_url, user.username, RESET_TTL_MINUTES),
    )
    # Deliberately NOT surfaced to the caller.
    log.info("password_reset.request issued user=%s mail_sent=%s", user.id, sent)
    return _RESET_GENERIC


@router.post("/password-reset/confirm")
async def password_reset_confirm(
    body: PasswordResetConfirm,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    import logging
    import time as _t

    log = logging.getLogger(__name__)
    from app.auth.admin import hash_password
    from app.models.db_user import PasswordResetToken

    if len(body.new_password or "") < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    client_ip = (request.client.host if request.client else "?") or "?"
    if not _reset_rate_ok(f"confirm:{client_ip}", _RESET_MAX_PER_IP_PER_HOUR * 3):
        raise HTTPException(429, "Too many attempts, try again later")

    row = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == _hash_reset_token(body.token or "")
        )
    )
    tok = row.scalars().first()
    now = _t.time()
    if tok is None or tok.used_at is not None or tok.expires_at < now:
        log.info("password_reset.confirm rejected ip=%s reason=%s", client_ip,
                 "missing" if tok is None else
                 ("used" if tok.used_at is not None else "expired"))
        raise HTTPException(400, "This reset link is invalid or has expired")

    urow = await db.execute(
        select(User).where(User.id == tok.user_id, User.deleted_at.is_(None))
    )
    user = urow.scalars().first()
    if user is None:
        raise HTTPException(400, "This reset link is invalid or has expired")

    user.password_hash = hash_password(body.new_password)
    user.last_user_edit_at = now
    tok.used_at = now
    # Spend every other outstanding token for this user as well.
    others = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
    )
    for other in others.scalars().all():
        other.used_at = now
    await db.commit()

    log.info("password_reset.confirm ok user=%s ip=%s", user.id, client_ip)
    return {"ok": True, "username": user.username}
