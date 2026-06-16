"""v5.7.6 — admin read endpoint for capability scout suggestions.

Returns the most recent ``mcp_capability_suggestion`` activity_log
rows so the MCP dashboard can render an "AI suggests you turn on…"
panel. Read-only — the operator acts on suggestions by editing per-key
policy via the v5.7.4 admin endpoint.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import require_admin
from app.models.database import get_db
from app.models.db import ActivityLog
from app.capability_scout.scout import EVENT_TYPE

router = APIRouter(prefix="/api/admin/mcp", tags=["admin-mcp"])


@router.get("/capability-suggestions")
async def list_capability_suggestions(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_admin),
    limit: int = Query(default=100, ge=1, le=500),
    api_key_id: Optional[str] = Query(default=None),
):
    """Most-recent suggestions first, optionally filtered to one key.

    Also returns a rollup ``by_tool`` so the dashboard can show "what
    are bots most often refusing" without a second query.
    """
    base = select(ActivityLog).where(ActivityLog.event_type == EVENT_TYPE)
    if api_key_id:
        base = base.where(ActivityLog.api_key_id == api_key_id)
    rs = await db.execute(
        base.order_by(ActivityLog.created_at.desc()).limit(limit)
    )
    rows = rs.scalars().all()

    items = []
    by_tool: dict[str, int] = {}
    for r in rows:
        meta = r.event_meta or {}
        tool = meta.get("suggested_tool") or "unknown"
        by_tool[tool] = by_tool.get(tool, 0) + 1
        items.append({
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "api_key_id": r.api_key_id,
            "provider_id": r.provider_id,
            "pattern_name": meta.get("pattern_name"),
            "suggested_tool": tool,
            "why": meta.get("why"),
            "matched_snippet": meta.get("matched_snippet"),
            "message": r.message,
        })

    # Total count (unbounded) for the panel header.
    total_rs = await db.execute(
        select(func.count(ActivityLog.id))
        .where(ActivityLog.event_type == EVENT_TYPE)
    )
    total = int(total_rs.scalar() or 0)

    return {
        "items": items,
        "by_tool": [{"suggested_tool": k, "count": v} for k, v in sorted(by_tool.items(), key=lambda kv: -kv[1])],
        "total_suggestions_lifetime": total,
        "shown": len(items),
    }
