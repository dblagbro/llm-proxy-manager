"""Log listing, SSE live tail, NDJSON export, clear — for the OAuth
capture package."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import require_admin, AdminUser
from app.models.database import AsyncSessionLocal, get_db
from app.models.db import OAuthCaptureLog

from app.api.oauth_capture.serializers import (
    _serialize_log_summary, _serialize_log_full,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/oauth-capture", tags=["oauth-capture"])


@router.get("/_log")
async def list_captures(
    profile: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    stmt = select(OAuthCaptureLog).order_by(OAuthCaptureLog.id.desc()).limit(min(limit, 500))
    if profile:
        stmt = stmt.where(OAuthCaptureLog.profile_name == profile)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_serialize_log_summary(r) for r in rows]


@router.get("/_log/{log_id}")
async def get_capture(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(select(OAuthCaptureLog).where(OAuthCaptureLog.id == log_id))
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Capture not found")
    return _serialize_log_full(r)


@router.get("/_log/stream/{profile_name}")
async def stream_captures(
    profile_name: str,
    _: AdminUser = Depends(require_admin),
):
    """SSE tail of captures for a given profile.

    Polls the log table every 500ms for new rows matching profile_name.
    Not fancy (no LISTEN/NOTIFY since we're on SQLite), but it's enough
    for the interactive capture wizard.
    """
    async def _tail():
        seen_max_id = 0
        yield f'event: ready\ndata: {{"profile":{json.dumps(profile_name)}}}\n\n'
        for _ in range(3600):  # cap at ~30 min so abandoned tails don't linger
            try:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(OAuthCaptureLog)
                        .where(
                            OAuthCaptureLog.profile_name == profile_name,
                            OAuthCaptureLog.id > seen_max_id,
                        )
                        .order_by(OAuthCaptureLog.id)
                        .limit(50)
                    )
                    rows = result.scalars().all()
                for r in rows:
                    seen_max_id = r.id
                    data = _serialize_log_summary(r)
                    yield f"data: {json.dumps(data)}\n\n"
            except Exception as exc:
                logger.warning("oauth_capture.stream_tick_failed %s", exc)
            await asyncio.sleep(0.5)
        yield 'event: end\ndata: {"reason":"timeout"}\n\n'

    return StreamingResponse(_tail(), media_type="text/event-stream")


@router.get("/_log/export/{profile_name}")
async def export_captures(
    profile_name: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """NDJSON dump of all captures for a given profile (offline reverse-eng)."""
    result = await db.execute(
        select(OAuthCaptureLog)
        .where(OAuthCaptureLog.profile_name == profile_name)
        .order_by(OAuthCaptureLog.id)
    )
    rows = result.scalars().all()

    async def _gen():
        for r in rows:
            yield (json.dumps(_serialize_log_full(r)) + "\n").encode()

    return StreamingResponse(_gen(), media_type="application/x-ndjson")


@router.delete("/_log")
async def clear_captures(
    profile: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Wipe captures. Pass ?profile=NAME to limit scope; no query wipes all."""
    stmt = sql_delete(OAuthCaptureLog)
    if profile:
        stmt = stmt.where(OAuthCaptureLog.profile_name == profile)
    result = await db.execute(stmt)
    await db.commit()
    return {"deleted": result.rowcount or 0, "profile": profile or "(all)"}
