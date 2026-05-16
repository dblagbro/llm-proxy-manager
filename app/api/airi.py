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

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.auth.admin import require_admin, AdminUser
from app.models.database import get_db
from app.airi.agent import run_airi_turn
from app.airi import rules

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


# ── v4.0 milestone 2 — rules layer + rule-sets ───────────────────────────────
# Rules are stored config in this milestone; they are wired to live supervisor
# behaviour in a later milestone. All endpoints admin-gated + feature-flagged.

async def _require_airi_enabled() -> None:
    if not settings.airi_enabled:
        raise HTTPException(status_code=404, detail="AIRI is disabled")


@router.get("/rulesets")
async def airi_list_rulesets(
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """All saved rule-sets (summary)."""
    return {"rulesets": await rules.list_rulesets(db)}


@router.get("/active-ruleset")
async def airi_active_ruleset(
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The currently active rule-set, with its rules."""
    return await rules.get_active_ruleset(db)


@router.post("/rulesets")
async def airi_save_ruleset(
    request: Request,
    user: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    """Save the active rule-set's rules under a new name. Body: ``{"name": ...}``."""
    body = await request.json()
    name = body.get("name") if isinstance(body, dict) else None
    result = await rules.save_as(db, name or "", created_by=user.username)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.post("/rulesets/restore-default")
async def airi_restore_default(
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    """Make the Default rule-set the active one."""
    result = await rules.restore_default(db)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.post("/rulesets/{ruleset_id}/activate")
async def airi_activate_ruleset(
    ruleset_id: str,
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    """Activate (restore) a saved rule-set."""
    result = await rules.activate(db, ruleset_id)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


@router.get("/rulesets/{ruleset_id}")
async def airi_get_ruleset(
    ruleset_id: str,
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    """One rule-set with its rules."""
    detail = await rules.get_ruleset_detail(db, ruleset_id)
    if detail is None:
        return JSONResponse({"detail": "rule-set not found"}, status_code=404)
    return detail


@router.patch("/rules/{rule_id}")
async def airi_update_rule(
    rule_id: str,
    request: Request,
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    """Edit a threshold rule's value. Body: ``{"value": <int>}``."""
    body = await request.json()
    value = body.get("value") if isinstance(body, dict) else None
    result = await rules.update_rule(db, rule_id, value)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result
