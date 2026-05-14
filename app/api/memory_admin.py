"""v3.9.6 (#267) Phase 9 — admin API for caller_memory + markers.

Endpoints
---------
- GET  /api/memory/keys/{api_key_id}                 → list memory entries
- GET  /api/memory/keys/{api_key_id}/{tag}           → single entry (default tag)
- GET  /api/memory/keys/{api_key_id}/{tag}/{conv_id} → single entry (scoped to conv)
- PUT  /api/memory/keys/{api_key_id}/{tag}           → upsert entry (operator-driven write)
- DELETE /api/memory/keys/{api_key_id}/{tag}         → soft-delete (tombstone)
- GET  /api/memory/markers/{api_key_id}              → list markers (with last_known_provider_id etc)
- POST /api/memory/markers/{marker_id}/clear-recovered → reset recovered_at so Phase 7 retries
- POST /api/memory/recover/{api_key_id}/{conv_id}/{tag} → manual recovery trigger

All endpoints require an admin session (require_admin dependency). The
feature is still gated on settings.caller_memory_enabled overall, but
admin endpoints work whether or not the flag is flipped — useful for
inspecting state before turning the feature on.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.models.db import CallerMemory, CallerMemoryMarker
from app.auth.admin import require_admin, AdminUser

router = APIRouter(prefix="/api/memory", tags=["caller-memory"])


class MemoryPut(BaseModel):
    content: str
    content_format: str = "text"
    conversation_id: Optional[str] = None
    source_provider_id: Optional[str] = None


def _serialize_memory(row: CallerMemory) -> dict:
    return {
        "id": row.id,
        "api_key_id": row.api_key_id,
        "conversation_id": row.conversation_id,
        "memory_tag": row.memory_tag,
        "content": row.content or "",
        "content_format": row.content_format,
        "updated_at": row.updated_at,
        "updated_by_node": row.updated_by_node,
        "source_provider_id": row.source_provider_id,
        "source_request_id": row.source_request_id,
        "deleted_at": row.deleted_at,
    }


def _serialize_marker(row: CallerMemoryMarker) -> dict:
    return {
        "id": row.id,
        "api_key_id": row.api_key_id,
        "conversation_id": row.conversation_id,
        "memory_tag": row.memory_tag,
        "first_seen_at": row.first_seen_at,
        "last_known_provider_id": row.last_known_provider_id,
        "last_known_external_ref": row.last_known_external_ref,
        "recovered_at": row.recovered_at,
        "deleted_at": row.deleted_at,
    }


# ── Memory CRUD ────────────────────────────────────────────────────


@router.get("/keys/{api_key_id}")
async def list_memory_for_key(
    api_key_id: str,
    limit: int = 100,
    include_deleted: bool = False,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    q = (
        select(CallerMemory)
        .where(CallerMemory.api_key_id == api_key_id)
        .order_by(desc(CallerMemory.updated_at))
        .limit(max(1, min(limit, 500)))
    )
    if not include_deleted:
        q = q.where(CallerMemory.deleted_at.is_(None))
    rows = (await db.execute(q)).scalars().all()
    return [_serialize_memory(r) for r in rows]


@router.put("/keys/{api_key_id}/{memory_tag}")
async def upsert_memory(
    api_key_id: str,
    memory_tag: str,
    body: MemoryPut,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    from app.memory.store import put
    entry = await put(
        db,
        api_key_id=api_key_id,
        content=body.content,
        conversation_id=body.conversation_id,
        memory_tag=memory_tag,
        content_format=body.content_format,
        source_provider_id=body.source_provider_id,
    )
    return {
        "ok": True,
        "api_key_id": entry.api_key_id,
        "memory_tag": entry.memory_tag,
        "conversation_id": entry.conversation_id,
        "updated_at": entry.updated_at,
    }


@router.delete("/keys/{api_key_id}/{memory_tag}")
async def delete_memory(
    api_key_id: str,
    memory_tag: str,
    conversation_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    from app.memory.store import delete as store_delete
    ok = await store_delete(
        db,
        api_key_id=api_key_id,
        conversation_id=conversation_id,
        memory_tag=memory_tag,
    )
    if not ok:
        raise HTTPException(404, "no live memory entry matches")
    return {"ok": True, "tombstoned": True}


# ── Markers ────────────────────────────────────────────────────────


@router.get("/markers/{api_key_id}")
async def list_markers(
    api_key_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    rows = (await db.execute(
        select(CallerMemoryMarker)
        .where(CallerMemoryMarker.api_key_id == api_key_id)
        .where(CallerMemoryMarker.deleted_at.is_(None))
        .order_by(desc(CallerMemoryMarker.first_seen_at))
        .limit(500)
    )).scalars().all()
    return [_serialize_marker(r) for r in rows]


@router.post("/markers/{marker_id}/clear-recovered")
async def clear_marker_recovered_at(
    marker_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Reset ``recovered_at`` so Phase 7 retries on next request. Useful
    after fixing a broken recovery handler — without this, a successful
    'recovery' that produced empty content would lock out further
    attempts."""
    row = (await db.execute(
        select(CallerMemoryMarker).where(CallerMemoryMarker.id == marker_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "marker not found")
    row.recovered_at = None
    await db.commit()
    return {"ok": True, "marker_id": marker_id, "recovered_at": None}


# ── Manual recovery trigger ────────────────────────────────────────


@router.post("/recover/{api_key_id}/{conversation_id}/{memory_tag}")
async def trigger_recovery(
    api_key_id: str,
    conversation_id: str,
    memory_tag: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Manually fire the Phase 7 recovery handler for this (key, conv,
    tag). Returns the recovered content (or None on failure). Useful
    for testing handler implementations end-to-end."""
    from app.memory.recover import maybe_recover_memory
    content = await maybe_recover_memory(
        db,
        api_key_id=api_key_id,
        conversation_id=conversation_id,
        memory_tag=memory_tag,
    )
    return {
        "ok": content is not None,
        "recovered_bytes": len(content) if content else 0,
        "content_preview": (content[:200] if content else None),
    }
