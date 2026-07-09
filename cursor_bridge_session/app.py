"""cursor-bridge-session FastAPI app.

v5.5.0 (2026-06-12) — SCAFFOLD only.
v5.5.1 (2026-07-02) — Playwright lifespan + PKCE drive
                      (this ship; container-code-only, not in compose yet).
v5.5.2 (deferred)   — HMAC-signed callback + 24h rotation cron.
v5.5.3 (deferred)   — Operator-facing UI panel in ProvidersPage.

Endpoints:
- GET  /healthz       liveness (LIVE since v5.5.0)
- GET  /api/status    JSON snapshot — reports Playwright / login state
- POST /api/rotate    force one rotation cycle
                      (v5.5.1 wires the PKCE + poll drive; returns
                      access_token + expiry on success)
- GET  /api/access-token   last-successful token (for llm-proxy2 poll)

v5.5.1 scope (what actually works after this ship):
- Persistent Chromium context initialized in lifespan
- POST /api/rotate runs a full PKCE + /loginDeepControl + /auth/poll flow
- State (last_rotation_at, logged_in, last_access_token) held in memory

v5.5.1 explicitly does NOT do:
- HMAC callback POST to llm-proxy2 (v5.5.2)
- Persistent state to disk (v5.5.2)
- Auto-rotation timer (v5.5.2)
- Operator UI (v5.5.3)
- Deploy into docker-compose (needs operator ops session)

Deploying this sidecar requires:
1. Adding a service block to /home/dblagbro/docker/docker-compose.yml
   (image built from this dir; volume mount for state; port 6080 for noVNC)
2. Building the image
3. Force-recreate on the target cluster
4. First-run: operator does one manual login via noVNC to seed cookies
5. Subsequent runs: POST /api/rotate silently drives PKCE from the seeded session
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException

logger = logging.getLogger(__name__)


# ── Configuration (env-driven) ──────────────────────────────────────
# Same URLs the llm-proxy2 cursor_oauth_flow uses. Kept in-sync with
# app/providers/cursor_oauth_flow.py:CURSOR_AUTH_POLL_URL.
CURSOR_LOGIN_URL = os.environ.get(
    "CURSOR_LOGIN_URL", "https://www.cursor.com/loginDeepControl",
)
CURSOR_AUTH_POLL_URL = os.environ.get(
    "CURSOR_AUTH_POLL_URL", "https://api2.cursor.sh/auth/poll",
)
CURSOR_POLL_UA = os.environ.get(
    "CURSOR_POLL_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Cursor/0.48.6 Chrome/132.0.6834.210 "
    "Electron/34.3.4 Safari/537.36",
)
BRIDGE_STATE_DIR = os.environ.get("BRIDGE_STATE_DIR", "/var/lib/bridge-state")
POLL_MAX_ATTEMPTS = int(os.environ.get("POLL_MAX_ATTEMPTS", "60"))
POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "2.0"))


# ── Module state (in-memory only in v5.5.1) ────────────────────────
_BOOT_TS = time.time()
_LAST_ROTATION_TS: Optional[float] = None
_LAST_ROTATION_STATUS: Optional[str] = None
_LAST_ACCESS_TOKEN: Optional[str] = None
_LAST_ACCESS_TOKEN_EXP: Optional[float] = None
_LOGGED_IN: bool = False

# Playwright handles held at process scope. Initialized in lifespan.
_playwright = None
_browser = None
_context = None
_rotate_lock: Optional[asyncio.Lock] = None


# ── PKCE helpers ────────────────────────────────────────────────────
# Mirrors the shape in app/providers/cursor_oauth_flow.py so the two
# paths generate compatible PKCE values.

def _b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _generate_pkce() -> tuple[str, str, str]:
    """Return ``(uuid, verifier, challenge)``. Uuid+verifier drive the
    poll; challenge goes on the /loginDeepControl URL."""
    uuid = _b64url_no_pad(secrets.token_bytes(16))
    verifier = _b64url_no_pad(secrets.token_bytes(43))
    challenge = _b64url_no_pad(hashlib.sha256(verifier.encode()).digest())
    return (uuid, verifier, challenge)


# ── Playwright context lifecycle ────────────────────────────────────

async def _init_playwright():
    """Launch a persistent Chromium context. On first boot, cookies dir
    is empty and operator must log in via noVNC once. Subsequent boots
    load the persisted state and can drive PKCE headlessly."""
    global _playwright, _browser, _context
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("playwright not installed; /api/rotate will 501")
        return

    _playwright = await async_playwright().start()
    # persistent_context = cookies survive container restarts
    user_data_dir = os.path.join(BRIDGE_STATE_DIR, "chromium-user-data")
    os.makedirs(user_data_dir, exist_ok=True)
    _context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
        user_agent=CURSOR_POLL_UA,
    )
    _browser = _context.browser
    # Test that a WorkOS session cookie exists — signals prior login.
    global _LOGGED_IN
    cookies = await _context.cookies()
    _LOGGED_IN = any(
        c.get("name") == "WorkosCursorSessionToken" for c in cookies
    )
    logger.info(
        "cursor-bridge-session Playwright initialized; logged_in=%s "
        "user_data_dir=%s", _LOGGED_IN, user_data_dir,
    )


async def _shutdown_playwright():
    global _playwright, _browser, _context
    try:
        if _context is not None:
            await _context.close()
        if _playwright is not None:
            await _playwright.stop()
    except Exception as e:
        logger.warning("playwright shutdown err=%s", e)
    finally:
        _playwright = None
        _browser = None
        _context = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _rotate_lock
    _rotate_lock = asyncio.Lock()
    logger.info(
        "cursor-bridge-session boot v5.5.1; state_dir=%s", BRIDGE_STATE_DIR,
    )
    try:
        await _init_playwright()
    except Exception as e:
        logger.warning("playwright init failed err=%s (continuing without)", e)
    yield
    await _shutdown_playwright()
    logger.info("cursor-bridge-session shutdown")


app = FastAPI(
    title="cursor-bridge-session",
    description=(
        "Stateful Cursor PKCE session holder for silent JWT rotation. "
        "v5.5.1 wires the Playwright drive + PKCE poll flow. Rotation "
        "cron + hmac callback land in v5.5.2."
    ),
    version="5.5.1",
    lifespan=lifespan,
)


# ── Endpoints ───────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "phase": "v5.5.1 (playwright + pkce drive)",
        "uptime_sec": round(time.time() - _BOOT_TS, 1),
        "playwright_ready": _context is not None,
    }


@app.get("/api/status")
async def status_snapshot() -> dict[str, Any]:
    return {
        "phase": "v5.5.1",
        "logged_in": _LOGGED_IN,
        "playwright_ready": _context is not None,
        "last_rotation_at": _LAST_ROTATION_TS,
        "last_rotation_status": _LAST_ROTATION_STATUS,
        "last_access_token_present": _LAST_ACCESS_TOKEN is not None,
        "last_access_token_expires_at": _LAST_ACCESS_TOKEN_EXP,
        "uptime_sec": round(time.time() - _BOOT_TS, 1),
    }


@app.get("/api/access-token")
async def get_access_token() -> dict[str, Any]:
    """llm-proxy2 pulls the latest successful rotation result from here.
    Returns 404 if no rotation has succeeded since boot."""
    if _LAST_ACCESS_TOKEN is None:
        raise HTTPException(status_code=404, detail="no rotation yet")
    return {
        "access_token": _LAST_ACCESS_TOKEN,
        "expires_at": _LAST_ACCESS_TOKEN_EXP,
        "rotated_at": _LAST_ROTATION_TS,
    }


async def _drive_pkce_once() -> dict[str, Any]:
    """Full rotation cycle: generate PKCE → open /loginDeepControl with
    challenge (uses the persistent WorkOS cookie) → poll api2.cursor.sh
    /auth/poll until it returns 200 with the fresh access token."""
    global _LAST_ROTATION_TS, _LAST_ROTATION_STATUS
    global _LAST_ACCESS_TOKEN, _LAST_ACCESS_TOKEN_EXP, _LOGGED_IN

    if _context is None:
        raise HTTPException(
            status_code=501,
            detail="playwright not initialized",
        )

    uuid, verifier, challenge = _generate_pkce()

    login_url = (
        f"{CURSOR_LOGIN_URL}?uuid={uuid}&challenge={challenge}"
    )
    page = await _context.new_page()
    try:
        # The persistent Chromium context carries the WorkOS session
        # cookie from a prior operator login; hitting /loginDeepControl
        # with challenge sets Cursor's server-side state, and the server
        # then makes the accessToken available at the /auth/poll endpoint
        # for the matching (uuid, verifier).
        await page.goto(login_url, wait_until="networkidle", timeout=30_000)
    finally:
        try:
            await page.close()
        except Exception:
            pass

    poll_url = (
        f"{CURSOR_AUTH_POLL_URL}?uuid={uuid}&verifier={verifier}"
    )
    headers = {"User-Agent": CURSOR_POLL_UA, "Accept": "*/*"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(POLL_MAX_ATTEMPTS):
            try:
                resp = await client.get(poll_url, headers=headers)
            except Exception as e:
                logger.warning(
                    "poll_attempt_failed attempt=%d err=%s", attempt, e,
                )
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue
            if resp.status_code == 200:
                data = resp.json()
                access_token = data.get("accessToken")
                auth_id = data.get("authId")
                if not access_token:
                    logger.warning(
                        "poll_200_no_access_token body=%r",
                        (resp.text or "")[:200],
                    )
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue
                # Decode JWT ``exp`` claim best-effort
                exp_ts = None
                try:
                    parts = access_token.split(".")
                    if len(parts) >= 2:
                        pad = "=" * (-len(parts[1]) % 4)
                        payload = base64.urlsafe_b64decode(parts[1] + pad)
                        import json as _json
                        exp_ts = float(_json.loads(payload).get("exp") or 0) or None
                except Exception:
                    pass
                _LAST_ACCESS_TOKEN = access_token
                _LAST_ACCESS_TOKEN_EXP = exp_ts
                _LAST_ROTATION_TS = time.time()
                _LAST_ROTATION_STATUS = "ok"
                _LOGGED_IN = True
                logger.info(
                    "rotate.success attempt=%d exp=%s auth_id_len=%d",
                    attempt, exp_ts, len(auth_id or ""),
                )
                return {
                    "ok": True,
                    "attempts": attempt + 1,
                    "access_token_len": len(access_token),
                    "expires_at": exp_ts,
                    "auth_id": auth_id,
                }
            elif resp.status_code in (401, 403):
                _LAST_ROTATION_STATUS = f"auth_failed:{resp.status_code}"
                _LOGGED_IN = False
                logger.warning(
                    "rotate.auth_failed status=%d — session cookie likely "
                    "expired; operator noVNC re-login required",
                    resp.status_code,
                )
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "cursor session cookie invalid — operator must "
                        "log in via noVNC once to reseed"
                    ),
                )
            # 202 (still pending) → keep polling
            await asyncio.sleep(POLL_INTERVAL_SEC)

    _LAST_ROTATION_STATUS = "poll_timeout"
    raise HTTPException(
        status_code=504,
        detail=(
            f"poll timeout after {POLL_MAX_ATTEMPTS} attempts × "
            f"{POLL_INTERVAL_SEC}s — /loginDeepControl may not have "
            f"completed inside the persistent context"
        ),
    )


@app.post("/api/rotate")
async def force_rotate() -> dict[str, Any]:
    """Operator-trigger + eventual cron-trigger for one full rotation.
    Serialized via _rotate_lock so concurrent callers don't stampede."""
    global _rotate_lock
    if _rotate_lock is None:
        raise HTTPException(status_code=503, detail="lock not initialized")
    async with _rotate_lock:
        return await _drive_pkce_once()
