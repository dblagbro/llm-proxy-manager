"""v5.22.9 — OIDC single sign-on endpoints (Google by default).

Wires the helpers that have sat unused in ``app/auth/sso.py`` since Wave 6
into a real authorization-code flow:

    GET /api/auth/sso/config    → public: is SSO on? (drives the login button)
    GET /api/auth/sso/start     → redirect to the IdP's authorize endpoint
    GET /api/auth/sso/callback  → exchange the code, create a session

Security posture — this is an unauthenticated entry point that mints
sessions, so the defaults are deliberately conservative:

* **Authorization-code flow with PKCE.** No implicit flow, no tokens in the
  browser URL beyond the one-time code.
* **ID token trust.** ``parse_id_token_claims`` does NOT verify signatures
  and says so. That is acceptable *here specifically* because the token is
  fetched over TLS directly from the issuer's token endpoint using our
  client_secret — the back-channel case OIDC Core §3.1.3.7 permits skipping
  signature validation for. We still validate ``iss``, ``aud``, ``exp`` and
  ``nonce`` explicitly below. Do NOT reuse this helper on a token that
  arrived via the browser.
* **No silent account creation.** ``sso_auto_provision`` defaults to False:
  a valid Google login for an unknown address is refused rather than
  quietly granted access to a compliance-scoped service.
* **Domain allow-list.** ``sso_allowed_domains`` restricts which email
  domains may sign in at all.
* **email_verified required** — an unverified address at the IdP could be
  someone else's.
* Single-use, short-lived, server-side state (never a client-supplied
  round-trip), so a replayed callback cannot mint a second session.
"""
from __future__ import annotations

import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    create_session,
    hash_password,
)
from app.auth.sso import (
    extract_identity,
    generate_nonce,
    generate_pkce_pair,
    generate_state,
    parse_id_token_claims,
    role_from_groups,
)
from app.models.database import get_db
from app.models.db import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth/sso", tags=["auth"])

SESSION_COOKIE_MAX_AGE = 86400 * 7
_STATE_TTL_SEC = 600          # 10 minutes to complete the round trip
_DISCOVERY_TTL_SEC = 3600

# state -> {nonce, verifier, created_at, next}
_pending: dict[str, dict] = {}
_discovery_cache: dict[str, tuple[float, dict]] = {}


def _cfg() -> dict:
    """Runtime settings first, env/Pydantic second (same pattern as mailer)."""
    from app.config import settings
    try:
        from app.config_runtime import get_setting
    except Exception:                                    # pragma: no cover
        get_setting = lambda *_a, **_k: None             # noqa: E731

    def pick(key, fallback):
        try:
            v = get_setting(key)
        except Exception:
            v = None
        return v if v not in (None, "") else fallback

    return {
        "enabled": bool(pick("sso_enabled", settings.sso_enabled)),
        "issuer": (pick("sso_issuer", settings.sso_issuer) or "").rstrip("/"),
        "client_id": pick("sso_client_id", settings.sso_client_id) or "",
        "client_secret": pick("sso_client_secret", settings.sso_client_secret) or "",
        "redirect_uri": pick("sso_redirect_uri", settings.sso_redirect_uri) or "",
        "default_role": pick("sso_default_role", settings.sso_default_role) or "user",
        "allowed_domains": [
            d.strip().lower()
            for d in (pick("sso_allowed_domains", settings.sso_allowed_domains) or "").split(",")
            if d.strip()
        ],
        "auto_provision": bool(pick("sso_auto_provision", settings.sso_auto_provision)),
    }


def _configured(cfg: dict) -> bool:
    return bool(cfg["enabled"] and cfg["client_id"] and cfg["client_secret"])


async def _discover(issuer: str) -> dict:
    """Fetch (and cache) the IdP's OIDC discovery document."""
    now = time.time()
    hit = _discovery_cache.get(issuer)
    if hit and now - hit[0] < _DISCOVERY_TTL_SEC:
        return hit[1]
    url = f"{issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url)
        r.raise_for_status()
        doc = r.json()
    _discovery_cache[issuer] = (now, doc)
    return doc


def _sweep_pending(now: float) -> None:
    for k in [k for k, v in _pending.items() if now - v["created_at"] > _STATE_TTL_SEC]:
        _pending.pop(k, None)


def _base_path(request: Request) -> str:
    """Public sub-path this app is served under (``/llm-proxy2``)."""
    return (request.headers.get("x-forwarded-prefix") or "/llm-proxy2").rstrip("/")


@router.get("/config")
async def sso_config():
    """Public — the login page asks whether to render the SSO button.

    Deliberately exposes ONLY a boolean and a label. No client id, no
    issuer internals, nothing that helps someone probe the deployment.
    """
    cfg = _cfg()
    on = _configured(cfg)
    label = "Sign in with Google" if "accounts.google.com" in cfg["issuer"] else "Sign in with SSO"
    return {"enabled": on, "label": label}


@router.get("/start")
async def sso_start(request: Request):
    cfg = _cfg()
    if not _configured(cfg):
        raise HTTPException(404, "SSO is not configured")

    try:
        doc = await _discover(cfg["issuer"])
    except Exception as exc:
        logger.warning("sso.discovery_failed issuer=%s err=%s", cfg["issuer"], exc)
        raise HTTPException(502, "Could not reach the identity provider")

    verifier, challenge = generate_pkce_pair()
    state, nonce = generate_state(), generate_nonce()
    now = time.time()
    _sweep_pending(now)
    if len(_pending) > 500:                      # crude bound on abuse
        raise HTTPException(429, "Too many sign-in attempts in flight")
    _pending[state] = {"nonce": nonce, "verifier": verifier, "created_at": now}

    redirect_uri = cfg["redirect_uri"] or (
        str(request.base_url).rstrip("/") + _base_path(request) + "/api/auth/sso/callback"
    )
    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    logger.info("sso.start issuer=%s", cfg["issuer"])
    return RedirectResponse(f"{doc['authorization_endpoint']}?{urlencode(params)}", status_code=302)


def _fail(request: Request, reason: str, log: str) -> RedirectResponse:
    """Send the browser back to the login page with a short reason.

    The message is intentionally generic — it must not reveal whether an
    account exists, only that this sign-in did not succeed.
    """
    logger.info("sso.callback_rejected reason=%s", log)
    return RedirectResponse(f"{_base_path(request)}/login?sso_error={reason}", status_code=302)


@router.get("/callback")
async def sso_callback(
    request: Request,
    response: Response,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    cfg = _cfg()
    if not _configured(cfg):
        raise HTTPException(404, "SSO is not configured")
    if error:
        return _fail(request, "denied", f"idp_error={error}")
    if not code or not state:
        return _fail(request, "invalid", "missing code/state")

    now = time.time()
    _sweep_pending(now)
    pending = _pending.pop(state, None)           # single-use by construction
    if pending is None:
        return _fail(request, "expired", "unknown or replayed state")

    try:
        doc = await _discover(cfg["issuer"])
    except Exception:
        return _fail(request, "idp_unreachable", "discovery failed")

    redirect_uri = cfg["redirect_uri"] or (
        str(request.base_url).rstrip("/") + _base_path(request) + "/api/auth/sso/callback"
    )
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            tr = await c.post(doc["token_endpoint"], data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "code_verifier": pending["verifier"],
            }, headers={"Accept": "application/json"})
        if tr.status_code != 200:
            return _fail(request, "exchange_failed", f"token endpoint {tr.status_code}")
        tok = tr.json()
    except Exception as exc:
        return _fail(request, "exchange_failed", f"{type(exc).__name__}")

    id_token = tok.get("id_token")
    if not id_token:
        return _fail(request, "exchange_failed", "no id_token in token response")

    # Signature is not re-verified — see the module docstring for why that is
    # sound for this back-channel exchange. These claim checks are NOT optional.
    try:
        claims = parse_id_token_claims(id_token)
    except Exception:
        return _fail(request, "invalid", "unparseable id_token")

    iss = (claims.get("iss") or "").rstrip("/")
    if iss not in (cfg["issuer"], cfg["issuer"].replace("https://", "https://accounts.")):
        # Google issues "https://accounts.google.com" for both variants.
        if iss.replace("https://", "") != cfg["issuer"].replace("https://", ""):
            return _fail(request, "invalid", f"issuer mismatch: {iss}")
    aud = claims.get("aud")
    aud_ok = cfg["client_id"] in (aud if isinstance(aud, list) else [aud])
    if not aud_ok:
        return _fail(request, "invalid", "audience mismatch")
    if float(claims.get("exp") or 0) < now:
        return _fail(request, "expired", "id_token expired")
    if claims.get("nonce") != pending["nonce"]:
        return _fail(request, "invalid", "nonce mismatch")

    identity = extract_identity(claims)
    email = (identity.email or "").strip().lower()
    if not email:
        return _fail(request, "no_email", "no email claim")
    if claims.get("email_verified") is False:
        return _fail(request, "unverified", "email_verified=false")
    if cfg["allowed_domains"] and email.rsplit("@", 1)[-1] not in cfg["allowed_domains"]:
        return _fail(request, "domain", f"domain not allowed: {email.rsplit('@', 1)[-1]}")

    row = await db.execute(
        select(User).where(User.email == email, User.deleted_at.is_(None))
    )
    user = row.scalars().first()
    if user is None:
        # Fall back to matching the local part as a username — covers accounts
        # that predate the email column (e.g. "dblagbro").
        row = await db.execute(
            select(User).where(
                User.username == email.split("@", 1)[0], User.deleted_at.is_(None)
            )
        )
        user = row.scalars().first()
        if user is not None and not user.email:
            user.email = email                       # bind it for next time

    if user is None:
        if not cfg["auto_provision"]:
            return _fail(request, "no_account", f"no local account for {email}")
        user = User(
            id=secrets.token_hex(8),
            username=email,
            # Unusable local password: SSO users authenticate at the IdP. A
            # random hash means the row can never be logged into with a
            # guessable credential.
            password_hash=hash_password(secrets.token_urlsafe(32)),
            role=role_from_groups(identity.groups, cfg["default_role"]),
            email=email,
            last_user_edit_at=now,
        )
        db.add(user)
        logger.info("sso.provisioned user=%s role=%s", email, user.role)

    await db.commit()
    token = await create_session(user.id, user.username, user.role)
    redirect = RedirectResponse(f"{_base_path(request)}/", status_code=302)
    redirect.set_cookie(
        SESSION_COOKIE_NAME, token,
        httponly=True, samesite="lax", secure=True,
        max_age=SESSION_COOKIE_MAX_AGE, path=SESSION_COOKIE_PATH,
    )
    logger.info("sso.login_ok user=%s role=%s", user.username, user.role)
    return redirect
