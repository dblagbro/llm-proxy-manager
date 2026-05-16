"""AIRI HTTP surface — v4.0 milestone 1.

``GET  /api/airi/status`` — is the feature enabled (drives whether the
                            Routing-page chat panel renders).
``POST /api/airi/chat``   — one read-only conversational turn, streamed
                            back as Server-Sent Events.

Admin-gated. Read-only — no routing mutation in milestone 1.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request, Depends
from fastapi.responses import StreamingResponse, JSONResponse

from app.config import settings
from app.auth.admin import require_admin, AdminUser
from app.airi.agent import run_airi_turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/airi", tags=["airi"])


@router.get("/status")
async def airi_status(_: AdminUser = Depends(require_admin)) -> dict:
    """Feature-flag probe — the Routing-page panel calls this and renders
    itself only when AIRI is enabled."""
    return {"enabled": bool(settings.airi_enabled)}


@router.post("/chat")
async def airi_chat(request: Request, _: AdminUser = Depends(require_admin)):
    """Run one AIRI turn. Body: ``{"messages": [{role, content}, ...]}`` —
    the full conversation so far, ending with the new user message.
    Response: a ``text/event-stream`` of ``status`` / ``message`` /
    ``error`` events, terminated by a ``done`` event."""
    if not settings.airi_enabled:
        return JSONResponse({"detail": "AIRI is disabled"}, status_code=404)

    body = await request.json()
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages:
        return JSONResponse({"detail": "messages[] is required"}, status_code=400)

    async def _stream():
        try:
            async for event, data in run_airi_turn(messages):
                yield f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
        except Exception as e:  # never leak a stack into the stream
            logger.warning("airi.chat_stream_failed err=%r", e)
            yield (
                b"event: error\ndata: "
                + json.dumps({"message": "AIRI hit an internal error."}).encode()
                + b"\n\n"
            )
        yield b"event: done\ndata: {}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
