"""Provider name dedup — boot-time migration + helpers.

Cluster-sync history occasionally produced duplicate active rows with the
same ``name`` (different ids on each node, both materialized after a sync
push). The duplicates fragmented per-provider history and confused the
admin UI. This module:

  1. ``dedup_providers_by_name(db)`` — boot-time migration. For each name
     with ≥2 active rows, keeps the highest-priority survivor (lowest
     ``priority`` value; ties broken by oldest ``created_at`` then lowest
     ``id``) and soft-deletes the rest via the existing tombstone path.
     Idempotent.
  2. The admin POST /api/providers + PUT /api/providers/{id} handlers
     reject creates/renames that would re-introduce a duplicate name.

Soft-deletes from the migration stamp ``last_user_edit_at`` so the
dedup decision propagates through cluster sync as an authoritative
edit — peers won't undo it.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Provider

logger = logging.getLogger(__name__)


async def dedup_providers_by_name(db: AsyncSession) -> list[dict]:
    """Soft-delete duplicate-name active providers; keep the top-priority survivor.

    Returns a list of {name, kept, dropped} dicts for whatever was deduped.
    Idempotent — second pass finds zero groups with >1 active row.
    """
    result = await db.execute(
        select(Provider).where(Provider.deleted_at.is_(None))
    )
    rows = result.scalars().all()

    by_name: dict[str, list[Provider]] = {}
    for r in rows:
        by_name.setdefault(r.name, []).append(r)

    actions: list[dict] = []
    now_dt = datetime.now(timezone.utc)
    now_ts = time.time()

    for name, group in by_name.items():
        if len(group) <= 1:
            continue
        group.sort(key=lambda p: (
            p.priority if p.priority is not None else 1_000_000,
            p.created_at or datetime.max.replace(tzinfo=timezone.utc),
            p.id,
        ))
        keep, drops = group[0], group[1:]
        for d in drops:
            d.deleted_at = now_dt
            d.enabled = False
            d.updated_at = now_dt
            # Stamp so the tombstone propagates as an authoritative edit.
            d.last_user_edit_at = now_ts
        actions.append({
            "name": name,
            "kept": keep.id,
            "dropped": [d.id for d in drops],
        })
        logger.warning(
            "providers.dedup_tombstoned name=%r kept=%s dropped=%s",
            name, keep.id, [d.id for d in drops],
        )

    if actions:
        await db.commit()
    return actions


async def name_is_taken(
    db: AsyncSession, name: str, exclude_id: str | None = None
) -> bool:
    """True if another active provider already uses this name."""
    stmt = select(Provider.id).where(
        Provider.name == name,
        Provider.deleted_at.is_(None),
    )
    if exclude_id is not None:
        stmt = stmt.where(Provider.id != exclude_id)
    return (await db.execute(stmt)).first() is not None
