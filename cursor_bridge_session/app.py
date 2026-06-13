"""cursor-bridge-session FastAPI app — v5.5.0 SCAFFOLD ONLY.

Endpoints stubbed:
- GET  /healthz       liveness; compose probe (LIVE)
- GET  /api/status    JSON snapshot (STUB)
- POST /api/rotate    force rotation cycle (STUB)
- /vnc/*              noVNC route (STUB — v5.5.1 wires it)

v5.5.1 will:
- Launch persistent Playwright Chromium during lifespan
- Wire /vnc/ as a websockify reverse-proxy to the supervisord stack
- Implement PKCE generator + /loginDeepControl drive + /auth/poll cycle
- Persist WorkOS cookies under BRIDGE_STATE_DIR

v5.5.2 will:
- Add the 24h rotation cron
- POST the new JWT to llm-proxy2 via HMAC-signed callback

v5.5.3 will:
- Operator-facing UI panel (Session health) in ProvidersPage
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

logger = logging.getLogger(__name__)


# Module state — held in memory; nothing persistent in v5.5.0 (that
# lands in v5.5.1 with the Playwright context).
_BOOT_TS = time.time()
_LAST_ROTATION_TS: float | None = None
_LAST_ROTATION_STATUS: str | None = None
_LOGGED_IN: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hook. v5.5.0 records boot timestamp only;
    v5.5.1 will launch Playwright + Chromium here."""
    logger.info(
        "cursor-bridge-session boot scaffold (v5.5.0); state_dir=%s",
        os.environ.get("BRIDGE_STATE_DIR"),
    )
    yield
    logger.info("cursor-bridge-session shutdown")


app = FastAPI(
    title="cursor-bridge-session",
    description=(
        "Stateful Cursor PKCE session holder for silent JWT rotation. "
        "Scaffold-only in v5.5.0 — Playwright + noVNC route + rotation "
        "cron land in v5.5.1-v5.5.2."
    ),
    version="5.5.0",
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Liveness probe. Compose healthcheck hits this.

    v5.5.0 reports scaffold status. Once v5.5.1 launches Playwright,
    this also reports whether Chromium is alive."""
    return {
        "status": "ok",
        "phase": "scaffold-v5.5.0",
        "uptime_sec": round(time.time() - _BOOT_TS, 1),
    }


@app.get("/api/status")
async def status_snapshot() -> dict[str, Any]:
    """Operator-facing session-health snapshot. v5.5.0 returns stubs
    — v5.5.2 fills in the real fields."""
    return {
        "phase": "scaffold-v5.5.0",
        "logged_in": _LOGGED_IN,
        "last_rotation_at": _LAST_ROTATION_TS,
        "last_rotation_status": _LAST_ROTATION_STATUS,
        "uptime_sec": round(time.time() - _BOOT_TS, 1),
        "note": (
            "Scaffold response. v5.5.1 will populate last_rotation_at "
            "and logged_in from the persistent Playwright context."
        ),
    }


@app.post("/api/rotate")
async def force_rotate() -> dict[str, Any]:
    """Operator-trigger force-rotate-now. v5.5.0 returns 501 — the
    actual PKCE drive lands in v5.5.1."""
    return {
        "ok": False,
        "error": "not-implemented-in-scaffold",
        "phase": "scaffold-v5.5.0",
        "note": "Rotation cycle ships in v5.5.1.",
    }


# v5.5.1 will add a /vnc proxy route here that forwards to
# websockify on localhost:6080. Not added in v5.5.0 to keep the
# scaffold ship surgical.
