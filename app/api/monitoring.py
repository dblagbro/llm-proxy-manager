"""Monitoring, metrics, and activity log endpoints."""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.models.database import get_db
from app.models.db import ActivityLog
from app.auth.admin import require_admin, AdminUser
from app.monitoring.activity import get_recent, subscribe, unsubscribe
from app.monitoring.metrics import get_provider_history, get_all_provider_summary
from app.monitoring.status import get_status_summary
from app.routing.circuit_breaker import get_all_states
from app.utils.timefmt import utc_iso

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


@router.get("/activity")
async def activity_log(
    limit: int = Query(100, le=1000),
    severity: Optional[str] = None,
    provider_id: Optional[str] = None,
    api_key_id: Optional[str] = Query(None, description="v3.0.35: filter to a single API key's events"),
    event_type: Optional[str] = Query(None, description="v3.0.35: filter to a single event_type (e.g. llm_request)"),
    since: Optional[str] = Query(None, description="v3.0.35: ISO 8601 timestamp lower bound (inclusive)"),
    until: Optional[str] = Query(None, description="v3.0.35: ISO 8601 timestamp upper bound (exclusive)"),
    sort: str = Query("desc", description="v3.0.35: 'desc' (default, newest first) or 'asc' (oldest first)"),
    before_id: Optional[int] = Query(None, description="Return events with id < this; cursor for paging back"),
    search: Optional[str] = Query(None, description="Substring match across message, provider_id, and metadata"),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v2.8.5 paginated + searchable; v3.0.35 adds per-key + per-event-type
    filters, ISO timestamp range, and asc/desc sort.

    ``before_id`` is the cursor for desc paging (pass the smallest id from
    the prior page). For asc, ``since`` is the natural cursor.
    """
    from sqlalchemy import cast, String
    from datetime import datetime as _dt

    if sort.lower() == "asc":
        order_clause = ActivityLog.created_at.asc()
    else:
        order_clause = desc(ActivityLog.created_at)

    query = select(ActivityLog).order_by(order_clause).limit(limit)

    if before_id is not None:
        query = query.where(ActivityLog.id < before_id)

    if provider_id:
        query = query.where(ActivityLog.provider_id == provider_id)

    if api_key_id:
        # v3.0.35: per-key filter — column already exists, just expose it.
        # Operator + DevinGPT both asked for this on 2026-05-01.
        query = query.where(ActivityLog.api_key_id == api_key_id)

    if event_type:
        query = query.where(ActivityLog.event_type == event_type)

    def _parse_iso(s: str):
        try:
            return _dt.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None

    if since:
        since_dt = _parse_iso(since)
        if since_dt is not None:
            query = query.where(ActivityLog.created_at >= since_dt)

    if until:
        until_dt = _parse_iso(until)
        if until_dt is not None:
            query = query.where(ActivityLog.created_at < until_dt)

    if severity:
        sev_list = [s.strip() for s in severity.split(",") if s.strip()]
        if len(sev_list) == 1:
            query = query.where(ActivityLog.severity == sev_list[0])
        elif sev_list:
            query = query.where(ActivityLog.severity.in_(sev_list))

    if search:
        # SQLite has no native FTS on JSON columns; do a case-insensitive
        # substring match against (message, provider_id, JSON-stringified
        # event_meta). Cheap for the common <100k row case; if tables get
        # big, add a dedicated FTS5 virtual table later.
        s = f"%{search}%"
        query = query.where(
            (ActivityLog.message.ilike(s))
            | (ActivityLog.provider_id.ilike(s))
            | (cast(ActivityLog.event_meta, String).ilike(s))
        )

    result = await db.execute(query)
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "event_type": r.event_type,
            "severity": r.severity,
            "message": r.message,
            "provider_id": r.provider_id,
            "api_key_id": r.api_key_id,  # v3.0.35: surface column for client-side correlation
            "timestamp": utc_iso(r.created_at),
            "metadata": r.event_meta,
        }
        for r in rows
    ]


@router.get("/activity/count")
async def activity_count(
    severity: Optional[str] = None,
    provider_id: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v2.8.5: total matching event count for the current filter — lets
    the UI show "showing 200 of N" so operators know how much they can
    page back through."""
    from sqlalchemy import func, cast, String
    query = select(func.count(ActivityLog.id))
    if provider_id:
        query = query.where(ActivityLog.provider_id == provider_id)
    if severity:
        sev_list = [s.strip() for s in severity.split(",") if s.strip()]
        if len(sev_list) == 1:
            query = query.where(ActivityLog.severity == sev_list[0])
        elif sev_list:
            query = query.where(ActivityLog.severity.in_(sev_list))
    if search:
        s = f"%{search}%"
        query = query.where(
            (ActivityLog.message.ilike(s))
            | (ActivityLog.provider_id.ilike(s))
            | (cast(ActivityLog.event_meta, String).ilike(s))
        )
    total = (await db.execute(query)).scalar() or 0
    return {"total": int(total)}


@router.get("/activity/stream")
async def activity_stream(_: AdminUser = Depends(require_admin)):
    """SSE stream of live activity events for the dashboard."""
    q = subscribe()

    async def _gen():
        # Send recent history first
        for event in get_recent(50):
            import json
            yield f"data: {json.dumps(event)}\n\n"
        # Then live events
        try:
            while True:
                import asyncio
                import json
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            unsubscribe(q)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get("/metrics")
async def metrics_summary(
    hours: int = Query(24, le=720),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    summary = await get_all_provider_summary(db, hours=hours)
    circuit_states = get_all_states()
    return {"hours": hours, "providers": summary, "circuit_breakers": circuit_states}


@router.get("/metrics/{provider_id}")
async def provider_metrics(
    provider_id: str,
    hours: int = Query(24, le=720),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    history = await get_provider_history(db, provider_id, hours=hours)
    return {"provider_id": provider_id, "hours": hours, "buckets": history}


@router.get("/status-pages")
async def external_status(_: AdminUser = Depends(require_admin)):
    return await get_status_summary()
