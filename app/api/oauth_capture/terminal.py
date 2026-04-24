"""
In-browser OAuth-capture terminal (v2.6.0).

Two endpoints connect the browser's xterm.js instance to a PTY running
inside the ``llm-proxy2-capture`` sidecar:

    POST /api/oauth-capture/_profiles/{name}/login
        Admin-authenticated. Picks the profile's preset, builds the CLI
        command + the env-var block (with the profile's capture URL +
        secret), asks the sidecar to spawn a PTY, returns a session_id.

    WS   /api/oauth-capture/_terminal/{session_id}
        Admin-authenticated via session cookie. Transparently relays
        text frames to the sidecar's ``/term/{session_id}`` WebSocket.
        Resize messages flow the same way (sent as JSON text frames).

Security posture
----------------
- The sidecar trusts anything on its network; the main proxy is the
  trust boundary.
- CLI command string is resolved from the preset, never from user input.
  The sidecar does a second-stage whitelist check on argv[0].
- One concurrent session globally (enforced by the sidecar). The UI
  surfaces a 409 with a "take over?" prompt.
- WS relay dies on either-side close. Sidecar's idle sweeper reaps
  stranded PTYs after 15 min.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import require_admin, AdminUser, _get_session
from app.config import settings
from app.models.database import get_db
from app.models.db import OAuthCaptureProfile

from app.api.oauth_capture.presets import PRESETS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/oauth-capture", tags=["oauth-capture"])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _sidecar_http_url() -> Optional[str]:
    """Return the configured sidecar HTTP URL, or None when disabled."""
    if not getattr(settings, "capture_sidecar_enabled", False):
        return None
    base = getattr(settings, "capture_sidecar_url", None)
    return base.rstrip("/") if base else None


def _sidecar_ws_url(http_url: str) -> str:
    """Swap http:// → ws:// (or https → wss)."""
    parsed = urlparse(http_url)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, "ws")
    return f"{scheme}://{parsed.netloc}{parsed.path}"


def _build_env_block(profile: OAuthCaptureProfile, proxy_base_url: str) -> dict[str, str]:
    """Build the env-var map the sidecar injects into the CLI's PTY.

    Every env var name declared on the preset is set to the capture URL
    for this profile (with the secret pre-applied as ``?cap=``). So
    ``claude login`` inside the sidecar hits our capture endpoint
    without the admin having to touch a shell.

    ``proxy_base_url`` is the main proxy's own URL as seen from the
    sidecar container — usually ``http://llm-proxy2:3000`` on the docker
    network.
    """
    preset = PRESETS.get(profile.preset or "")
    if preset is None:
        return {}

    capture_url = f"{proxy_base_url.rstrip('/')}/api/oauth-capture/{profile.name}"
    secret = profile.secret or ""
    full_url = f"{capture_url}?cap={secret}" if secret else capture_url

    env = {name: full_url for name in preset.env_var_names}
    env["LLM_PROXY_CAPTURE_PROFILE"] = profile.name
    return env


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/_profiles/{name}/login")
async def start_login_session(
    name: str,
    takeover: bool = False,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Spawn the profile's ``login_cmd`` in the sidecar's PTY."""
    sidecar = _sidecar_http_url()
    if sidecar is None:
        raise HTTPException(503, "Sidecar disabled or not configured")

    profile_row = (await db.execute(
        select(OAuthCaptureProfile).where(OAuthCaptureProfile.name == name)
    )).scalar_one_or_none()
    if profile_row is None:
        raise HTTPException(404, f"Profile {name!r} not found")
    if not profile_row.enabled:
        raise HTTPException(409, "Enable the profile before starting a terminal session")

    preset = PRESETS.get(profile_row.preset or "")
    if preset is None or not preset.login_cmd:
        raise HTTPException(
            400,
            f"Preset {profile_row.preset!r} has no login_cmd — in-browser terminal "
            "not supported for this vendor. Use the manual env-var flow.",
        )

    proxy_base = getattr(settings, "main_container_base_url", None) or "http://llm-proxy2:3000"
    env = _build_env_block(profile_row, proxy_base)

    session_id = uuid.uuid4().hex
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            url = f"{sidecar}/spawn" + ("?takeover=1" if takeover else "")
            resp = await client.post(url, json={
                "session_id": session_id,
                "command": preset.login_cmd,
                "env": env,
            })
            data = resp.json()
            if resp.status_code == 409:
                return {
                    "error": "session_in_use",
                    "active_session_id": data.get("active_session_id"),
                    "age_seconds": data.get("age_seconds"),
                }
            if resp.status_code >= 400:
                raise HTTPException(502, f"Sidecar rejected spawn: {data}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Sidecar unreachable: {e}")

    logger.info("terminal.spawned profile=%s session=%s", name, session_id[:8])
    return {"session_id": session_id, "profile": name, "command": preset.login_cmd}


@router.post("/_terminal/{session_id}/kill")
async def kill_session(
    session_id: str,
    _: AdminUser = Depends(require_admin),
):
    sidecar = _sidecar_http_url()
    if sidecar is None:
        raise HTTPException(503, "Sidecar disabled")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(f"{sidecar}/kill/{session_id}")
            return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Sidecar unreachable: {e}")


@router.websocket("/_terminal/{session_id}")
async def terminal_ws(websocket: WebSocket, session_id: str):
    """Bidirectional PTY relay.

    Admin auth is enforced by reading the session cookie directly — we
    can't use FastAPI's HTTP ``Depends(require_admin)`` on a WebSocket
    route because the admin layer currently only speaks HTTP.
    """
    token = websocket.cookies.get("admin_session") or websocket.cookies.get("session")
    if not token:
        await websocket.close(code=4401)
        return
    session_info = await _get_session(token)
    if not session_info:
        await websocket.close(code=4401)
        return

    sidecar_http = _sidecar_http_url()
    if sidecar_http is None:
        await websocket.close(code=4503)
        return
    sidecar_ws_base = _sidecar_ws_url(sidecar_http)
    target = f"{sidecar_ws_base}/term/{session_id}"

    await websocket.accept()

    try:
        async with websockets.connect(target, ping_interval=25) as upstream:
            browser_to_sidecar = asyncio.create_task(
                _relay_browser_to_sidecar(websocket, upstream)
            )
            sidecar_to_browser = asyncio.create_task(
                _relay_sidecar_to_browser(upstream, websocket)
            )
            done, pending = await asyncio.wait(
                {browser_to_sidecar, sidecar_to_browser},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except websockets.exceptions.InvalidStatus as e:
        code = getattr(e.response, "status_code", 0)
        logger.info("terminal.ws_upstream_rejected session=%s status=%s", session_id[:8], code)
        try:
            await websocket.close(code=4404)
        except Exception:
            pass
    except (OSError, websockets.exceptions.WebSocketException) as e:
        logger.warning("terminal.ws_upstream_failed session=%s err=%s", session_id[:8], e)
        try:
            await websocket.close(code=4502)
        except Exception:
            pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("terminal.ws_error session=%s err=%s", session_id[:8], e)
        try:
            await websocket.close(code=4500)
        except Exception:
            pass


async def _relay_browser_to_sidecar(browser: WebSocket, upstream) -> None:
    try:
        while True:
            msg = await browser.receive_text()
            await upstream.send(msg)
    except WebSocketDisconnect:
        try:
            await upstream.close()
        except Exception:
            pass
    except Exception:
        pass


async def _relay_sidecar_to_browser(upstream, browser: WebSocket) -> None:
    try:
        async for msg in upstream:
            # websockets yields str for text frames, bytes for binary
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8", errors="replace")
            try:
                await browser.send_text(msg)
            except Exception:
                break
    finally:
        try:
            await browser.close()
        except Exception:
            pass
