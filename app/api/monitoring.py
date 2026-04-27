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

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


@router.get("/activity")
async def activity_log(
    limit: int = Query(100, le=500),
    severity: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    query = select(ActivityLog).order_by(desc(ActivityLog.created_at)).limit(limit)
    if severity:
        # Comma-separated list, e.g. ?severity=warning,error → IN (...)
        sev_list = [s.strip() for s in severity.split(",") if s.strip()]
        if len(sev_list) == 1:
            query = query.where(ActivityLog.severity == sev_list[0])
        elif sev_list:
            query = query.where(ActivityLog.severity.in_(sev_list))
    result = await db.execute(query)
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "event_type": r.event_type,
            "severity": r.severity,
            "message": r.message,
            "provider_id": r.provider_id,
            "timestamp": r.created_at.isoformat() if r.created_at else None,
            "metadata": r.event_meta,
        }
        for r in rows
    ]


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
