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

import httpx
from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.auth.admin import require_admin, AdminUser
from app.models.database import get_db, AsyncSessionLocal
from app.airi.agent import run_airi_turn
from app.airi import rules, history

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/airi", tags=["airi"])

# v4.2 — voice input. Audio is forwarded to the whisper-bridge sidecar.
_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # ~a few minutes of opus
_TRANSCRIBE_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0)


@router.get("/status")
async def airi_status(_: AdminUser = Depends(require_admin)) -> dict:
    """Feature-flag probe — the Routing-page panel calls this and renders
    itself only when AIRI is enabled. ``voice_enabled`` (v4.2) drives the
    mic button."""
    return {
        "enabled": bool(settings.airi_enabled),
        "voice_enabled": bool(settings.airi_enabled and settings.airi_voice_enabled),
    }


@router.post("/transcribe")
async def airi_transcribe(
    file: UploadFile = File(...),
    _: AdminUser = Depends(require_admin),
):
    """Transcribe an operator's spoken chat input (v4.2 milestone 1).

    Forwards the audio to the self-hosted whisper-bridge sidecar and returns
    ``{"text": ...}``. The audio is streamed through — never persisted,
    never logged. The operator reviews the text before sending it as a chat
    message, so the normal PII/guard path still applies."""
    if not settings.airi_enabled:
        return JSONResponse({"detail": "AIRI is disabled"}, status_code=404)
    if not settings.airi_voice_enabled:
        return JSONResponse({"detail": "AIRI voice input is disabled"}, status_code=404)

    audio = await file.read()
    if not audio:
        return JSONResponse({"error": "an audio 'file' is required"}, status_code=400)
    if len(audio) > _MAX_AUDIO_BYTES:
        return JSONResponse({"error": "audio too large"}, status_code=413)

    bridge = (settings.airi_whisper_bridge_url or "").rstrip("/")
    if not bridge:
        return JSONResponse({"error": "voice transcription is not configured"},
                            status_code=503)
    try:
        async with httpx.AsyncClient(timeout=_TRANSCRIBE_TIMEOUT) as client:
            r = await client.post(
                f"{bridge}/transcribe",
                files={"file": (file.filename or "audio.webm", audio,
                                file.content_type or "application/octet-stream")},
                headers={"Authorization":
                         f"Bearer {settings.airi_whisper_bridge_token}"},
            )
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # never leak a stack — the panel falls back to typing
        logger.warning("airi.transcribe failed err=%r", e)
        return JSONResponse({"error": "transcription is unavailable right now"},
                            status_code=502)
    return {"text": (data.get("text") or "").strip()}


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _last_user_text(messages: list) -> str:
    """The newest plain-text user message — the title seed + the persisted turn."""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user" \
                and isinstance(m.get("content"), str):
            return m["content"]
    return ""


@router.post("/chat")
async def airi_chat(request: Request, user: AdminUser = Depends(require_admin)):
    """Run one AIRI turn. Body: ``{"messages": [{role, content}, ...],
    "conversation_id": <str|null>}`` — the full conversation so far, ending
    with the new user message. Response: a ``text/event-stream`` opening with
    a ``conversation`` event (the thread id, new or continued), then
    ``status`` / ``proposal`` / ``message`` / ``error`` events, terminated by
    ``done``.

    History is persisted (M5): the user turn before the agent runs, AIRI's
    answer after it — each in its own short DB session, never held across the
    LLM call (ARCH-A). A persistence failure is logged and swallowed; the
    chat itself never breaks because history could not be written."""
    if not settings.airi_enabled:
        return JSONResponse({"detail": "AIRI is disabled"}, status_code=404)

    body = await request.json()
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages:
        return JSONResponse({"detail": "messages[] is required"}, status_code=400)
    conversation_id = body.get("conversation_id") if isinstance(body, dict) else None
    user_text = _last_user_text(messages)

    async def _stream():
        # 1. Persist the user turn — own session, before the agent loop.
        conv_id = conversation_id
        try:
            async with AsyncSessionLocal() as db:
                conv_id = await history.start_turn(
                    db, user_id=user.username,
                    conversation_id=conversation_id, user_text=user_text,
                )
        except Exception as e:
            logger.warning("airi.history_start_failed err=%r", e)
        yield _sse("conversation", {"conversation_id": conv_id})

        # 2. Run the turn, streaming events through; capture the final answer.
        answer = None
        try:
            async for event, data in run_airi_turn(messages, actor=user.username):
                if event == "message":
                    answer = data.get("text")
                yield _sse(event, data)
        except Exception as e:  # never leak a stack into the stream
            logger.warning("airi.chat_stream_failed err=%r", e)
            yield _sse("error", {"message": "AIRI hit an internal error."})

        # 3. Persist AIRI's answer — own session, after the loop closed.
        if conv_id and answer:
            try:
                async with AsyncSessionLocal() as db:
                    await history.record_assistant(
                        db, conversation_id=conv_id, text=answer)
            except Exception as e:
                logger.warning("airi.history_record_failed err=%r", e)
        yield _sse("done", {})

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


# ── v4.0 milestone 3 — proposals (propose -> apply / reject / revert) ─────────

@router.get("/proposals")
async def airi_list_proposals(
    status: str | None = None,
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recent AIRI proposals — the change audit trail. Optional ``?status=``."""
    from app.airi import proposals
    return {"proposals": await proposals.list_proposals(db, status=status)}


@router.get("/proposals/{proposal_id}")
async def airi_get_proposal(
    proposal_id: str,
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    from app.airi import proposals
    p = await proposals.get_proposal(db, proposal_id)
    if p is None:
        return JSONResponse({"detail": "proposal not found"}, status_code=404)
    return p


@router.post("/proposals/{proposal_id}/apply")
async def airi_apply_proposal(
    proposal_id: str,
    user: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    """Approve + apply a pending proposal."""
    from app.airi import proposals
    result = await proposals.apply_proposal(db, proposal_id, applied_by=user.username)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.post("/proposals/{proposal_id}/reject")
async def airi_reject_proposal(
    proposal_id: str,
    user: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    """Reject a pending proposal."""
    from app.airi import proposals
    result = await proposals.reject_proposal(db, proposal_id, decided_by=user.username)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.post("/proposals/{proposal_id}/revert")
async def airi_revert_proposal(
    proposal_id: str,
    user: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    """Undo an applied proposal — restore the prior-state snapshot."""
    from app.airi import proposals
    result = await proposals.revert_proposal(db, proposal_id, decided_by=user.username)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


# ── v4.0 milestone 4 — automation kill switch + scheduled-rule registry ───────

@router.get("/automation")
async def airi_get_automation(
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
) -> dict:
    """Current state of the scheduled-rule automation kill switch."""
    from app.airi import evaluator
    return {
        "automation_enabled": evaluator.is_automation_enabled(),
        "evaluator_interval_sec": int(settings.airi_evaluator_interval_sec),
    }


@router.post("/automation")
async def airi_set_automation(
    request: Request,
    user: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
):
    """Flip the automation kill switch. Body: ``{"enabled": <bool>}``."""
    from app.airi import evaluator
    body = await request.json()
    enabled = bool(body.get("enabled")) if isinstance(body, dict) else False
    evaluator.set_automation(enabled)
    logger.info("airi.automation set enabled=%s by=%s", enabled, user.username)
    return {"ok": True, "automation_enabled": enabled}


@router.post("/rules/{rule_id}/toggle")
async def airi_toggle_rule(
    rule_id: str,
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    """Enable / disable a scheduled or monitor rule."""
    result = await rules.toggle_rule(db, rule_id)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


# ── v4.0 milestone 5 — conversation history (per-user) + cross-user search ────

@router.get("/conversations")
async def airi_list_conversations(
    user: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The calling operator's own AIRI conversations, most-recent first."""
    return {"conversations": await history.list_conversations(db, user_id=user.username)}


@router.get("/search")
async def airi_search(
    q: str = "",
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Full-text search across EVERY operator's AIRI conversations
    (decision #5 — the shared change-coordination history)."""
    return {"query": q, "results": await history.search_messages(db, query=q)}


@router.get("/conversations/{conversation_id}")
async def airi_get_conversation(
    conversation_id: str,
    _: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    """One conversation with its full transcript. Any operator may open any
    conversation — history is shared for change coordination."""
    detail = await history.get_conversation(db, conversation_id)
    if detail is None:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    return detail


# ── v4.0.3 — per-user notification preferences ───────────────────────────────

@router.get("/notification-prefs")
async def airi_get_notification_pref(
    user: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The calling operator's own AIRI-notification subscription."""
    from app.airi import notify_prefs
    return await notify_prefs.get_pref(db, user.username)


@router.put("/notification-prefs")
async def airi_set_notification_pref(
    request: Request,
    user: AdminUser = Depends(require_admin),
    __: None = Depends(_require_airi_enabled),
    db: AsyncSession = Depends(get_db),
):
    """Save the calling operator's notification subscription. Body:
    ``{email, enabled, categories: {monitor, automation}, min_severity}``."""
    from app.airi import notify_prefs
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "a JSON object body is required"}, status_code=400)
    result = await notify_prefs.set_pref(
        db, user.username,
        email=body.get("email"),
        enabled=bool(body.get("enabled", True)),
        categories=body.get("categories"),
        min_severity=body.get("min_severity") or "warning",
    )
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result
