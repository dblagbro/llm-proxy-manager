"""v5.11.0 — SSE live-tail of activity_log rows for the admin dashboard.

Pattern inspired by the upstream-review of ccflare (snipeship/ccflare,
2026-06-29 audit): their ``/api/requests/stream`` exposes a live SSE
feed of the request log so the admin UI can show "what's happening
right now" without polling.

Implementation here:
- ``GET /api/admin/requests/stream`` returns ``text/event-stream``.
- Each ``activity_log`` row inserted after the connection opens is
  broadcast as one SSE ``data:`` event (JSON body).
- Long-poll: server queries ``WHERE id > last_seen`` every 1.5s with the
  watchdog already on (so a closed browser cancels the handler cleanly,
  same v5.7.17 contract that covers /v1/messages).
- Admin auth via ``require_admin``.
- Optional ``?event_type=`` query filter (substring match) for tailing
  one slice (e.g. only ``proxy_tool.dedupe_skip`` to satisfy DevinGPT's
  2026-06-25 monitoring-tab ask).

Why polling instead of pub/sub: SQLite + multi-process (uvicorn worker
fan-out) means an in-process queue won't see writes from other workers.
A 1.5s poll against an indexed ``id`` column is cheap (a few ms) and
correct under the multi-worker topology we run. If the table volume
grows past tens of inserts/sec we can revisit.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import AsyncSessionLocal
from app.models.db import ActivityLog
from app.utils.disconnect_watchdog import watch_for_disconnect

router = APIRouter(prefix="/api/admin", tags=["admin", "monitoring"])


_POLL_INTERVAL_SEC = 1.5
_HEARTBEAT_INTERVAL_SEC = 25.0  # SSE comment frame to keep proxies from idle-closing
_MAX_BATCH = 200  # bound per-poll batch so a backlog doesn't blow up the wire


def _row_to_dict(row: ActivityLog) -> dict:
    return {
        "id": row.id,
        "event_type": row.event_type,
        "severity": row.severity,
        "message": row.message,
        "provider_id": row.provider_id,
        "api_key_id": row.api_key_id,
        "event_meta": row.event_meta or {},
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
    }


async def _seed_last_id() -> int:
    """Return the current max id so the first poll cycle only emits rows
    inserted AFTER the connection opened. Clients that want history can
    page the existing /api/admin/activity-log endpoint first."""
    async with AsyncSessionLocal() as db:
        rs = await db.execute(select(ActivityLog.id).order_by(desc(ActivityLog.id)).limit(1))
        v = rs.scalar()
        return int(v or 0)


async def _next_batch(db: AsyncSession, last_id: int, event_type: Optional[str]) -> list[ActivityLog]:
    q = select(ActivityLog).where(ActivityLog.id > last_id).order_by(ActivityLog.id).limit(_MAX_BATCH)
    if event_type:
        # Substring match — callers can pass "proxy_tool" to tail all
        # MCP-related events, or "circuit_break" for CB activity, etc.
        q = q.where(ActivityLog.event_type.like(f"%{event_type}%"))
    rs = await db.execute(q)
    return list(rs.scalars().all())


@router.get("/requests/stream")
async def requests_stream(
    request: Request,
    _watchdog: None = Depends(watch_for_disconnect),
    _admin: AdminUser = Depends(require_admin),
    event_type: Optional[str] = None,
):
    """Live-tail SSE of activity_log inserts. Long-poll under the hood.

    Cancellation: the v5.7.17 disconnect watchdog cancels the generator
    when the client closes the EventSource — no DB session leaks even if
    a dashboard tab sits open for hours then gets closed.
    """
    last_id = await _seed_last_id()
    poll_int = _POLL_INTERVAL_SEC

    async def _gen():
        nonlocal last_id
        # Initial connect ack — tells the client the stream is live and
        # lets the EventSource error path distinguish "endpoint down"
        # from "connected, just no events yet."
        yield "event: connected\ndata: {}\n\n"

        elapsed_since_heartbeat = 0.0
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    rows = await _next_batch(db, last_id, event_type)
                for row in rows:
                    last_id = row.id
                    payload = json.dumps(_row_to_dict(row), default=str)
                    yield f"data: {payload}\n\n"
                if rows:
                    elapsed_since_heartbeat = 0.0
                else:
                    elapsed_since_heartbeat += poll_int
                    if elapsed_since_heartbeat >= _HEARTBEAT_INTERVAL_SEC:
                        # SSE comment frame — keeps nginx + intermediaries
                        # from closing the connection during quiet periods.
                        yield ": keepalive\n\n"
                        elapsed_since_heartbeat = 0.0
            except asyncio.CancelledError:
                # Watchdog cancelled us because the client disconnected.
                # Don't re-raise — let the generator close cleanly so
                # FastAPI returns a clean 200 close to anything still
                # reading.
                return
            except Exception:
                # Don't crash the stream on a transient DB hiccup. Pause
                # and try again on the next tick.
                pass
            try:
                await asyncio.sleep(poll_int)
            except asyncio.CancelledError:
                return

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tell nginx (when fronting us) to not buffer SSE.
            "X-Accel-Buffering": "no",
        },
    )


# ── v5.20.5 — per-row detail endpoint ─────────────────────────────────
# Companion to /requests/stream: click a row in the stream, get the full
# expanded context via a stable URL (permalinkable).
#
# Ported from ccflare's /api/requests/detail (2026-06-30 peer-comparison
# roadmap). Their shape: one row + correlated events within a small
# time window + the provider/key context that isn't inline on the
# activity_log row itself.

_CORRELATION_WINDOW_SEC = 30
_CORRELATED_MAX_ROWS = 50
_REDACTED_KEY_LEN = 8


def _redact_key_hash(full_id: Optional[str]) -> Optional[str]:
    """API key IDs are the SHA-256 prefix of the key — they identify
    the key but knowing more than the first ~8 chars adds no diagnostic
    value + increases surface if the response is logged elsewhere.
    Reveal only the first 8 hex chars, mask the rest."""
    if not full_id:
        return None
    return full_id[:_REDACTED_KEY_LEN] + "..." if len(full_id) > _REDACTED_KEY_LEN else full_id


async def _correlated_events(
    db: AsyncSession, row: ActivityLog,
) -> list[dict]:
    """Return activity_log rows within ±_CORRELATION_WINDOW_SEC of ``row``
    that share the same api_key_id AND (provider_id if present). Bundled
    context for a single logical LLM request that produced multiple
    audit_log rows (llm_request + cost_split + refusal_detected + etc.)."""
    from datetime import timedelta
    if row.created_at is None:
        return []
    lo = row.created_at - timedelta(seconds=_CORRELATION_WINDOW_SEC)
    hi = row.created_at + timedelta(seconds=_CORRELATION_WINDOW_SEC)
    q = select(ActivityLog).where(
        ActivityLog.id != row.id,
        ActivityLog.created_at >= lo,
        ActivityLog.created_at <= hi,
    )
    if row.api_key_id:
        q = q.where(ActivityLog.api_key_id == row.api_key_id)
    if row.provider_id:
        q = q.where(
            (ActivityLog.provider_id == row.provider_id)
            | (ActivityLog.provider_id.is_(None))
        )
    q = q.order_by(ActivityLog.id).limit(_CORRELATED_MAX_ROWS)
    rs = await db.execute(q)
    out = []
    for r in rs.scalars().all():
        out.append({
            "id": r.id,
            "event_type": r.event_type,
            "severity": r.severity,
            "message": r.message,
            "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
            "delta_ms": (
                int((r.created_at - row.created_at).total_seconds() * 1000)
                if r.created_at else None
            ),
        })
    return out


async def _provider_summary(db: AsyncSession, provider_id: Optional[str]) -> Optional[dict]:
    """Small provider-context bundle for the detail response. Redacts
    anything credential-shaped."""
    if not provider_id:
        return None
    try:
        from app.models.db import Provider
        rs = await db.execute(select(Provider).where(Provider.id == provider_id))
        p = rs.scalar_one_or_none()
        if p is None:
            return None
        return {
            "id": p.id,
            "name": p.name,
            "provider_type": getattr(p, "provider_type", None),
            "enabled": bool(getattr(p, "enabled", False)),
            "cost_class": getattr(p, "cost_class", None),
        }
    except Exception:
        return None


async def _apikey_summary(db: AsyncSession, api_key_id: Optional[str]) -> Optional[dict]:
    """Small api_key-context bundle. Never returns the full hash; the
    redacted 8-char prefix is enough to identify + non-sensitive."""
    if not api_key_id:
        return None
    try:
        from app.models.db import ApiKey
        rs = await db.execute(select(ApiKey).where(ApiKey.id == api_key_id))
        k = rs.scalar_one_or_none()
        if k is None:
            return None
        return {
            "id_prefix": _redact_key_hash(k.id),
            "name": k.name,
            "key_type": getattr(k, "key_type", None),
            "enabled": bool(getattr(k, "enabled", False)),
        }
    except Exception:
        return None


@router.get("/requests/detail/{activity_log_id}")
async def request_detail(
    activity_log_id: int,
    _admin: AdminUser = Depends(require_admin),
):
    """Per-row expanded detail: the row itself + provider/key context +
    correlated activity_log events in a ±30s window.

    Use case: operator sees a row in /requests/stream, wants the full
    context. Returns 404 for unknown IDs (protects existence-check
    scans against a numeric row ID). Admin-only.

    Response shape (v5.20.5):

    ```json
    {
      "row": { "id": ..., "event_type": ..., ..., "event_meta": {...} },
      "provider": { "id": ..., "name": ..., "provider_type": ... } | null,
      "api_key":  { "id_prefix": "abcd1234...", "name": ..., ... } | null,
      "correlated_events": [
        { "id": ..., "event_type": ..., "delta_ms": ..., ... },
        ...
      ],
      "correlation_window_sec": 30
    }
    ```
    """
    from fastapi import HTTPException
    async with AsyncSessionLocal() as db:
        rs = await db.execute(
            select(ActivityLog).where(ActivityLog.id == activity_log_id)
        )
        row = rs.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="activity_log row not found")

        provider = await _provider_summary(db, row.provider_id)
        api_key = await _apikey_summary(db, row.api_key_id)
        correlated = await _correlated_events(db, row)

    return {
        "row": _row_to_dict(row),
        "provider": provider,
        "api_key": api_key,
        "correlated_events": correlated,
        "correlation_window_sec": _CORRELATION_WINDOW_SEC,
    }
