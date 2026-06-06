"""
Structured activity log — writes to DB and emits to in-memory ring buffer
for real-time streaming to the web dashboard.
"""
import asyncio
import logging
from collections import deque
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ActivityLog
from app.utils.timefmt import utc_iso

logger = logging.getLogger(__name__)

# Ring buffer for SSE streaming to the dashboard (last 200 events)
_ring: deque[dict] = deque(maxlen=200)
_subscribers: list[asyncio.Queue] = []


async def log_event(
    db: AsyncSession,
    event_type: str,
    message: str,
    severity: str = "info",
    provider_id: Optional[str] = None,
    api_key_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
):
    # v5.1.0 / Batch C1 (compliance panic button) — check the
    # persistent toggle BEFORE writing the DB row OR fanning out to
    # SSE subscribers. When off, the call is a complete no-op (no
    # disk write, no in-memory copy, no SSE leak). The audit row for
    # the toggle flip itself rides a separate code path (compliance_
    # policy_changes table) so the operator's proof-of-flip is
    # preserved even when activity logging is silent.
    from app.monitoring.logging_controls import is_logging_enabled
    if not await is_logging_enabled(db):
        return
    entry = ActivityLog(
        event_type=event_type,
        severity=severity,
        message=message,
        provider_id=provider_id,
        api_key_id=api_key_id,
        event_meta=metadata or {},
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    record = {
        "id": entry.id,
        "event_type": event_type,
        "severity": severity,
        "message": message,
        "provider_id": provider_id,
        "timestamp": utc_iso(entry.created_at) or (datetime.utcnow().isoformat() + "Z"),
        "metadata": metadata or {},
    }
    _ring.append(record)
    for q in list(_subscribers):
        try:
            q.put_nowait(record)
        except asyncio.QueueFull:
            pass


def get_recent(limit: int = 100) -> list[dict]:
    return list(_ring)[-limit:]


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue):
    try:
        _subscribers.remove(q)
    except ValueError:
        pass
