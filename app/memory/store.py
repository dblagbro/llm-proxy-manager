"""v3.8.8 (#267) Phase 3 — caller memory read/write layer.

Three-tier storage:
1. **Redis** (hot cache) — local per-node, namespaced by
   ``llmproxy:mem:{api_key_id}:{conv_id}:{tag}``. Populated on first
   read from SQLite; invalidated on writes.
2. **SQLite** (durable king-store) — ``caller_memory`` table.
   Source of truth + cluster-sync transport.
3. **In-process dict** (fallback) — used when both Redis and SQLite
   are unavailable (e.g. mid-shutdown). Same pattern as
   ``app/cot/session.py``.

Cluster replication: writes go to local SQLite immediately; the
existing cluster-sync (60s cadence) propagates rows to peer nodes
via ``_apply_caller_memory`` (in ``app/cluster/sync.py``). Reads are
always LOCAL — no network hop in the hot path.

Phase 3 ships the store + admin write helpers. No request-time
injection yet — Phase 4 handles that.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_KEY_PREFIX = "llmproxy:mem:"
_redis_client = None
_redis_ok = False
_fallback: dict[str, dict] = {}  # last-resort if Redis + SQLite both down


@dataclass
class MemoryEntry:
    """Operator-facing view of a memory row. Excludes internal
    bookkeeping fields (id, updated_by_node, etc.)."""
    api_key_id: str
    conversation_id: Optional[str]
    memory_tag: str
    content: str
    content_format: str
    updated_at: float
    source_provider_id: Optional[str] = None


def _key(api_key_id: str, conversation_id: Optional[str], memory_tag: str) -> str:
    """Build the Redis cache key. ``None`` conversation_id is encoded
    as the literal string ``__default__`` to avoid colliding with
    callers who actually pass that as a tag value."""
    conv = conversation_id if conversation_id is not None else "__default__"
    return f"{_KEY_PREFIX}{api_key_id}:{conv}:{memory_tag}"


async def _get_redis():
    """Lazy-init the Redis client. Returns None when redis_url is unset
    or the client can't be reached. Mirrors ``app/cot/session.py``."""
    global _redis_client, _redis_ok
    if _redis_client is not None:
        return _redis_client if _redis_ok else None
    try:
        from app.config import settings
        if not settings.redis_url:
            return None
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await _redis_client.ping()
        _redis_ok = True
        logger.info("caller_memory: Redis connected")
    except Exception as e:
        logger.info(f"caller_memory: Redis unavailable ({e}), using SQLite + in-process fallback")
        _redis_ok = False
    return _redis_client if _redis_ok else None


async def get(
    db,
    api_key_id: str,
    conversation_id: Optional[str] = None,
    memory_tag: str = "default",
) -> Optional[MemoryEntry]:
    """Read one memory entry. Redis-first; falls back to SQLite, then
    to the in-process dict. Returns None if no live row exists (or
    the row is tombstoned via deleted_at)."""
    key = _key(api_key_id, conversation_id, memory_tag)
    # Redis hot path
    r = await _get_redis()
    if r is not None:
        try:
            raw = await r.get(key)
            if raw:
                d = json.loads(raw)
                if d.get("deleted_at") is None:
                    return MemoryEntry(
                        api_key_id=d["api_key_id"],
                        conversation_id=d.get("conversation_id"),
                        memory_tag=d.get("memory_tag", "default"),
                        content=d.get("content", ""),
                        content_format=d.get("content_format", "text"),
                        updated_at=float(d.get("updated_at", 0)),
                        source_provider_id=d.get("source_provider_id"),
                    )
        except Exception as e:
            logger.warning(f"caller_memory: Redis get failed ({e}), falling through")

    # SQLite durable path
    from sqlalchemy import select
    from app.models.db import CallerMemory
    q = (
        select(CallerMemory)
        .where(CallerMemory.api_key_id == api_key_id)
        .where(CallerMemory.memory_tag == memory_tag)
    )
    if conversation_id is None:
        q = q.where(CallerMemory.conversation_id.is_(None))
    else:
        q = q.where(CallerMemory.conversation_id == conversation_id)
    row = (await db.execute(q)).scalar_one_or_none()
    if row is None or row.deleted_at is not None:
        # In-process fallback (last resort)
        fb = _fallback.get(key)
        if fb and fb.get("deleted_at") is None:
            return MemoryEntry(**{k: v for k, v in fb.items() if k != "deleted_at"})
        return None

    entry = MemoryEntry(
        api_key_id=row.api_key_id,
        conversation_id=row.conversation_id,
        memory_tag=row.memory_tag,
        content=row.content or "",
        content_format=row.content_format or "text",
        updated_at=row.updated_at,
        source_provider_id=row.source_provider_id,
    )
    # Backfill the Redis cache for next read
    if r is not None:
        try:
            await r.set(key, json.dumps({**entry.__dict__, "deleted_at": None}))
        except Exception:
            pass
    return entry


async def put(
    db,
    *,
    api_key_id: str,
    content: str,
    conversation_id: Optional[str] = None,
    memory_tag: str = "default",
    content_format: str = "text",
    source_provider_id: Optional[str] = None,
    source_request_id: Optional[str] = None,
) -> MemoryEntry:
    """Write/update one memory entry.

    Writes SQLite first (durable + cluster-sync transport), then
    invalidates the Redis cache (next read re-populates from SQLite).
    Also updates the marker row so back-pressure recovery has the
    provenance trail.
    """
    from sqlalchemy import select
    from app.config import settings
    from app.models.db import CallerMemory, CallerMemoryMarker
    now = time.time()
    node_id = getattr(settings, "cluster_node_id", None)

    # Upsert content row
    q = (
        select(CallerMemory)
        .where(CallerMemory.api_key_id == api_key_id)
        .where(CallerMemory.memory_tag == memory_tag)
    )
    if conversation_id is None:
        q = q.where(CallerMemory.conversation_id.is_(None))
    else:
        q = q.where(CallerMemory.conversation_id == conversation_id)
    existing = (await db.execute(q)).scalar_one_or_none()
    if existing is None:
        row = CallerMemory(
            api_key_id=api_key_id,
            conversation_id=conversation_id,
            memory_tag=memory_tag,
            content=content,
            content_format=content_format,
            updated_at=now,
            updated_by_node=node_id,
            source_provider_id=source_provider_id,
            source_request_id=source_request_id,
            deleted_at=None,
        )
        db.add(row)
    else:
        existing.content = content
        existing.content_format = content_format
        existing.updated_at = now
        existing.updated_by_node = node_id
        if source_provider_id:
            existing.source_provider_id = source_provider_id
        if source_request_id:
            existing.source_request_id = source_request_id
        existing.deleted_at = None
        row = existing

    # Upsert marker (back-pressure recovery anchor)
    mq = (
        select(CallerMemoryMarker)
        .where(CallerMemoryMarker.api_key_id == api_key_id)
        .where(CallerMemoryMarker.memory_tag == memory_tag)
    )
    if conversation_id is None:
        mq = mq.where(CallerMemoryMarker.conversation_id.is_(None))
    else:
        mq = mq.where(CallerMemoryMarker.conversation_id == conversation_id)
    marker = (await db.execute(mq)).scalar_one_or_none()
    if marker is None:
        db.add(CallerMemoryMarker(
            api_key_id=api_key_id,
            conversation_id=conversation_id,
            memory_tag=memory_tag,
            first_seen_at=now,
            last_known_provider_id=source_provider_id,
            last_known_external_ref=None,
            recovered_at=None,
            deleted_at=None,
        ))
    else:
        if source_provider_id:
            marker.last_known_provider_id = source_provider_id

    await db.commit()

    # Invalidate Redis cache so the next read pulls the new SQLite row.
    key = _key(api_key_id, conversation_id, memory_tag)
    r = await _get_redis()
    if r is not None:
        try:
            await r.delete(key)
        except Exception:
            pass

    return MemoryEntry(
        api_key_id=api_key_id,
        conversation_id=conversation_id,
        memory_tag=memory_tag,
        content=content,
        content_format=content_format,
        updated_at=now,
        source_provider_id=source_provider_id,
    )


async def delete(
    db,
    api_key_id: str,
    conversation_id: Optional[str] = None,
    memory_tag: str = "default",
) -> bool:
    """Soft-delete one memory entry. Returns True if a row was found
    and tombstoned, False otherwise. Tombstone propagates via
    cluster sync (peer nodes adopt the deleted_at via LWW)."""
    from sqlalchemy import select
    from app.models.db import CallerMemory
    now = time.time()
    q = (
        select(CallerMemory)
        .where(CallerMemory.api_key_id == api_key_id)
        .where(CallerMemory.memory_tag == memory_tag)
    )
    if conversation_id is None:
        q = q.where(CallerMemory.conversation_id.is_(None))
    else:
        q = q.where(CallerMemory.conversation_id == conversation_id)
    row = (await db.execute(q)).scalar_one_or_none()
    if row is None or row.deleted_at is not None:
        return False
    row.deleted_at = now
    row.updated_at = now  # bump so LWW propagates the tombstone
    await db.commit()

    # Invalidate Redis
    r = await _get_redis()
    if r is not None:
        try:
            await r.delete(_key(api_key_id, conversation_id, memory_tag))
        except Exception:
            pass
    return True


async def list_for_key(db, api_key_id: str, limit: int = 100) -> list[MemoryEntry]:
    """List all live memory entries for an api_key (admin-side view)."""
    from sqlalchemy import select, desc
    from app.models.db import CallerMemory
    rows = (await db.execute(
        select(CallerMemory)
        .where(CallerMemory.api_key_id == api_key_id)
        .where(CallerMemory.deleted_at.is_(None))
        .order_by(desc(CallerMemory.updated_at))
        .limit(limit)
    )).scalars().all()
    return [
        MemoryEntry(
            api_key_id=r.api_key_id,
            conversation_id=r.conversation_id,
            memory_tag=r.memory_tag,
            content=r.content or "",
            content_format=r.content_format or "text",
            updated_at=r.updated_at,
            source_provider_id=r.source_provider_id,
        )
        for r in rows
    ]
