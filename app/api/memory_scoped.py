"""v3.9.12 (#267 follow-up) — api-key-scoped caller-memory CRUD.

Sibling to ``memory_admin.py``, which requires an admin session. This
module exposes the same caller-memory operations under ``/v1/memory/*``
authenticated by the API key whose memory is being touched — no admin
session needed.

Use cases this unblocks:
- Hub-Claude purging memory on room archival (the original ask: hub
  team wanted a way to clean up on `room_guid` archive without holding
  an admin session in their automation)
- Any caller that wants to inspect / seed / clear its own memory
  programmatically as part of normal traffic

Security note: the api_key implicitly identifies the tenant. There is
no cross-key access — calling ``DELETE /v1/memory/conversations/X``
with key A can ONLY delete rows where ``caller_memory.api_key_id == A``.
This is the same isolation that inject/extract enforce on the
hot-request path.

Endpoints:
- GET    /v1/memory/conversations                  → list all conv_ids
                                                     with at least one
                                                     live memory row for
                                                     this api_key
- GET    /v1/memory/conversations/{conv_id}        → list all (tag, content)
                                                     rows for this conv
- PUT    /v1/memory/conversations/{conv_id}/{tag}  → upsert memory
- DELETE /v1/memory/conversations/{conv_id}        → tombstone every
                                                     memory row + marker
                                                     for this conv
- DELETE /v1/memory/conversations/{conv_id}/{tag}  → tombstone one row
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.models.db import CallerMemory, CallerMemoryMarker
from app.auth.keys import ApiKeyRecord, resolve_api_key_dep

router = APIRouter(prefix="/v1/memory", tags=["caller-memory-scoped"])

_api_key_dep = resolve_api_key_dep()


class MemoryPut(BaseModel):
    content: str
    content_format: str = "text"


def _serialize(row: CallerMemory) -> dict:
    return {
        "conversation_id": row.conversation_id,
        "memory_tag": row.memory_tag,
        "content": row.content or "",
        "content_format": row.content_format,
        "updated_at": row.updated_at,
        "source_provider_id": row.source_provider_id,
    }


@router.get("/conversations")
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(_api_key_dep),
):
    """List distinct conversation_ids with at least one live row for
    this api_key. Useful for hub-style callers iterating "what do I
    have memory for?" before deciding to purge."""
    rows = (await db.execute(
        select(CallerMemory.conversation_id, CallerMemory.memory_tag, CallerMemory.updated_at)
        .where(CallerMemory.api_key_id == key.id)
        .where(CallerMemory.deleted_at.is_(None))
        .order_by(desc(CallerMemory.updated_at))
        .limit(500)
    )).all()
    # Group by conversation_id, accumulate tag list + max updated_at
    by_conv: dict = {}
    for conv, tag, updated_at in rows:
        bucket = by_conv.setdefault(conv, {"conversation_id": conv, "tags": [], "updated_at": 0.0})
        bucket["tags"].append(tag)
        bucket["updated_at"] = max(bucket["updated_at"], float(updated_at or 0))
    return sorted(
        list(by_conv.values()), key=lambda x: x["updated_at"], reverse=True,
    )


@router.get("/conversations/{conversation_id}")
async def list_memory_for_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(_api_key_dep),
):
    rows = (await db.execute(
        select(CallerMemory)
        .where(CallerMemory.api_key_id == key.id)
        .where(CallerMemory.conversation_id == conversation_id)
        .where(CallerMemory.deleted_at.is_(None))
        .order_by(desc(CallerMemory.updated_at))
    )).scalars().all()
    return [_serialize(r) for r in rows]


@router.put("/conversations/{conversation_id}/{memory_tag}")
async def upsert_scoped_memory(
    conversation_id: str,
    memory_tag: str,
    body: MemoryPut,
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(_api_key_dep),
):
    from app.memory.store import put
    entry = await put(
        db,
        api_key_id=key.id,
        content=body.content,
        conversation_id=conversation_id,
        memory_tag=memory_tag,
        content_format=body.content_format,
    )
    return {
        "ok": True,
        "conversation_id": entry.conversation_id,
        "memory_tag": entry.memory_tag,
        "updated_at": entry.updated_at,
    }


@router.delete("/conversations/{conversation_id}")
async def delete_entire_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(_api_key_dep),
):
    """Tombstone every memory row for this (api_key, conversation_id),
    across all memory_tags, plus the marker. Equivalent of "this room
    is archived — drop our memory of it."

    Returns the count of rows tombstoned. Idempotent — re-running
    returns 0 (nothing left to tombstone).
    """
    now = time.time()
    affected = 0
    # CallerMemory rows
    rows = (await db.execute(
        select(CallerMemory)
        .where(CallerMemory.api_key_id == key.id)
        .where(CallerMemory.conversation_id == conversation_id)
        .where(CallerMemory.deleted_at.is_(None))
    )).scalars().all()
    for row in rows:
        row.deleted_at = now
        row.updated_at = now  # bump for LWW cluster propagation
        affected += 1
    # Marker rows
    markers = (await db.execute(
        select(CallerMemoryMarker)
        .where(CallerMemoryMarker.api_key_id == key.id)
        .where(CallerMemoryMarker.conversation_id == conversation_id)
        .where(CallerMemoryMarker.deleted_at.is_(None))
    )).scalars().all()
    for m in markers:
        m.deleted_at = now
    await db.commit()

    # Invalidate Redis cache
    try:
        from app.memory.store import _get_redis, _key
        r = await _get_redis()
        if r is not None:
            for row in rows:
                try:
                    await r.delete(_key(row.api_key_id, conversation_id, row.memory_tag))
                except Exception:
                    pass
    except Exception:
        pass

    return {
        "ok": True,
        "conversation_id": conversation_id,
        "memory_rows_tombstoned": affected,
        "markers_tombstoned": len(markers),
    }


@router.delete("/conversations/{conversation_id}/{memory_tag}")
async def delete_one_tag(
    conversation_id: str,
    memory_tag: str,
    db: AsyncSession = Depends(get_db),
    key: ApiKeyRecord = Depends(_api_key_dep),
):
    from app.memory.store import delete as store_delete
    ok = await store_delete(
        db,
        api_key_id=key.id,
        conversation_id=conversation_id,
        memory_tag=memory_tag,
    )
    if not ok:
        raise HTTPException(404, "no live memory entry matches")
    return {
        "ok": True,
        "conversation_id": conversation_id,
        "memory_tag": memory_tag,
        "tombstoned": True,
    }
