"""AIRI conversation history — v4.0 milestone 5.

Persists AIRI chat threads. History is per-user (an operator sees their own
threads listed), but search spans **every** user's conversations — decision
#5: the shared history is how two operators avoid making opposing routing
changes blind.

ARCH-A discipline: every function takes a caller-supplied session and never
holds it across an LLM call — the chat endpoint persists the user turn and
the assistant turn in two separate short sessions, bracketing the agent loop.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import select, func, desc

from app.models.db import AiriConversation, AiriMessage

logger = logging.getLogger(__name__)

_TITLE_MAX = 70


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _title_from(text: str) -> str:
    """A conversation title: the first user line, collapsed and truncated.
    Deterministic — no extra LLM call just to name a thread."""
    line = " ".join((text or "").split()).strip()
    if not line:
        return "(untitled)"
    return line if len(line) <= _TITLE_MAX else line[: _TITLE_MAX - 1].rstrip() + "…"


async def start_turn(db, *, user_id: str, conversation_id: str | None,
                     user_text: str) -> str:
    """Create-or-continue a conversation and append the user message.
    Returns the conversation id (newly minted when ``conversation_id`` is
    None or does not resolve)."""
    # Explicit microsecond timestamps — SQLite's CURRENT_TIMESTAMP default is
    # only second-granular, so two threads opened in the same second would
    # sort non-deterministically. _now() carries microseconds.
    ts = _now()
    conv = None
    if conversation_id:
        conv = await db.get(AiriConversation, conversation_id)
    if conv is None:
        conv = AiriConversation(
            id=secrets.token_hex(8), user_id=user_id or "operator",
            title=_title_from(user_text), created_at=ts, updated_at=ts,
        )
        db.add(conv)
        await db.flush()
    else:
        conv.updated_at = ts  # bump so the thread sorts to the top
    db.add(AiriMessage(
        id=secrets.token_hex(8), conversation_id=conv.id,
        role="user", content=user_text or "", created_at=ts,
    ))
    await db.commit()
    return conv.id


async def record_assistant(db, *, conversation_id: str, text: str) -> None:
    """Append AIRI's final answer to a conversation."""
    conv = await db.get(AiriConversation, conversation_id)
    if conv is None:
        return
    ts = _now()
    conv.updated_at = ts
    db.add(AiriMessage(
        id=secrets.token_hex(8), conversation_id=conversation_id,
        role="assistant", content=text or "", created_at=ts,
    ))
    await db.commit()


def _conv_summary(conv: AiriConversation, msg_count: int) -> dict:
    return {
        "id": conv.id, "user_id": conv.user_id,
        "title": conv.title or "(untitled)", "message_count": msg_count,
        "created_at": str(conv.created_at) if conv.created_at else None,
        "updated_at": str(conv.updated_at) if conv.updated_at else None,
    }


async def list_conversations(db, *, user_id: str, limit: int = 50) -> list[dict]:
    """An operator's own threads, most-recently-active first."""
    counts = dict((await db.execute(
        select(AiriMessage.conversation_id, func.count())
        .group_by(AiriMessage.conversation_id)
    )).all())
    rows = (await db.execute(
        select(AiriConversation)
        .where(AiriConversation.user_id == user_id)
        .order_by(desc(AiriConversation.updated_at))
        .limit(limit)
    )).scalars().all()
    return [_conv_summary(c, counts.get(c.id, 0)) for c in rows]


async def get_conversation(db, conversation_id: str) -> dict | None:
    """One conversation with its full transcript, oldest message first."""
    conv = await db.get(AiriConversation, conversation_id)
    if conv is None:
        return None
    msgs = (await db.execute(
        select(AiriMessage)
        .where(AiriMessage.conversation_id == conversation_id)
        .order_by(AiriMessage.created_at, AiriMessage.id)
    )).scalars().all()
    out = _conv_summary(conv, len(msgs))
    out["messages"] = [
        {"role": m.role, "content": m.content or "",
         "created_at": str(m.created_at) if m.created_at else None}
        for m in msgs
    ]
    return out


def _snippet(content: str, terms: list[str]) -> str:
    """A short excerpt of ``content`` centred on the first matching term."""
    text = " ".join((content or "").split())
    low = text.lower()
    at = -1
    for t in terms:
        at = low.find(t)
        if at >= 0:
            break
    if at < 0:
        return text[:160]
    start = max(0, at - 60)
    end = min(len(text), at + 100)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


async def search_messages(db, *, query: str, limit: int = 20) -> list[dict]:
    """Cross-user full-text search over conversation messages. Every term
    must appear (AND); results are most-recent first. This is the shared
    coordination surface — it spans **all** users' AIRI history."""
    terms = [t.lower() for t in (query or "").split() if t.strip()]
    if not terms:
        return []
    stmt = select(AiriMessage, AiriConversation).join(
        AiriConversation, AiriMessage.conversation_id == AiriConversation.id,
    )
    for t in terms:
        stmt = stmt.where(func.lower(AiriMessage.content).like(f"%{t}%"))
    stmt = stmt.order_by(desc(AiriMessage.created_at)).limit(limit)
    rows = (await db.execute(stmt)).all()
    return [
        {
            "conversation_id": conv.id,
            "conversation_title": conv.title or "(untitled)",
            "user_id": conv.user_id,
            "role": msg.role,
            "snippet": _snippet(msg.content, terms),
            "at": str(msg.created_at) if msg.created_at else None,
        }
        for msg, conv in rows
    ]
