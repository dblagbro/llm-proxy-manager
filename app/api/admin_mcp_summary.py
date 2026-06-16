"""v5.7.5 — Aggregation endpoint for the MCP dashboard panel.

Single GET returns everything the dashboard needs:
- mcp_tool_calls counts by tool_name + ok, last 24h
- per-tool latency p50/p95, last 24h
- per-key call counts last 24h
- live tool inventory (name + 1-line description) from the FastMCP root
- live worker heartbeat for the MCP-related sub-system (if any)

Read-only; admin-gated.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db


router = APIRouter(prefix="/api/admin/mcp", tags=["admin", "mcp"])


@router.get("/summary")
async def mcp_summary(
    db: AsyncSession = Depends(get_db),
    _admin: AdminUser = Depends(require_admin),
) -> dict[str, Any]:
    """Single-call aggregator for the dashboard."""
    out: dict[str, Any] = {
        "tools_live": [],
        "calls_by_tool_24h": [],
        "calls_by_key_24h": [],
        "latency_by_tool_24h": [],
        "total_calls_24h": 0,
        "total_errors_24h": 0,
    }

    # ── Live tool inventory (from FastMCP root) ─────────────────────
    try:
        from app.main import _mcp_sub_app  # type: ignore
        if _mcp_sub_app is not None:
            mcp = _mcp_sub_app.state.mcp
            tools = await mcp.list_tools()
            out["tools_live"] = [
                {
                    "name": getattr(t, "name", ""),
                    "description": (
                        getattr(t, "description", "") or ""
                    )[:200],
                }
                for t in tools
            ]
    except Exception as exc:
        out["tools_live_error"] = repr(exc)

    # ── Aggregations from mcp_tool_calls (last 24h) ────────────────
    try:
        # Total counts
        row = (await db.execute(
            text(
                "SELECT COUNT(*), SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) "
                "FROM mcp_tool_calls "
                "WHERE created_at >= datetime('now','-24 hours')"
            )
        )).first()
        if row:
            out["total_calls_24h"] = int(row[0] or 0)
            out["total_errors_24h"] = int(row[1] or 0)

        # By tool: count + errors
        rs = await db.execute(
            text(
                "SELECT tool_name, COUNT(*) AS n, "
                "       SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS e "
                "FROM mcp_tool_calls "
                "WHERE created_at >= datetime('now','-24 hours') "
                "GROUP BY tool_name ORDER BY n DESC LIMIT 20"
            )
        )
        out["calls_by_tool_24h"] = [
            {"tool_name": r[0], "count": int(r[1]), "errors": int(r[2] or 0)}
            for r in rs.fetchall()
        ]

        # By key
        rs = await db.execute(
            text(
                "SELECT api_key_id, COUNT(*) AS n "
                "FROM mcp_tool_calls "
                "WHERE created_at >= datetime('now','-24 hours') "
                "AND api_key_id IS NOT NULL "
                "GROUP BY api_key_id ORDER BY n DESC LIMIT 20"
            )
        )
        out["calls_by_key_24h"] = [
            {"api_key_id": r[0], "count": int(r[1])} for r in rs.fetchall()
        ]

        # Latency by tool — SQLite percentile_disc would be nice but
        # not available; sort + index manually.
        rs = await db.execute(
            text(
                "SELECT tool_name, latency_ms "
                "FROM mcp_tool_calls "
                "WHERE created_at >= datetime('now','-24 hours') "
                "AND ok=1 "
                "ORDER BY tool_name, latency_ms"
            )
        )
        by_tool: dict[str, list[int]] = {}
        for r in rs.fetchall():
            by_tool.setdefault(r[0], []).append(int(r[1] or 0))
        latency_rows = []
        for name, lats in by_tool.items():
            if not lats:
                continue
            p50 = lats[len(lats) // 2]
            p95 = lats[max(0, int(len(lats) * 0.95) - 1)]
            latency_rows.append({
                "tool_name": name,
                "p50_ms": p50,
                "p95_ms": p95,
                "n": len(lats),
            })
        out["latency_by_tool_24h"] = sorted(
            latency_rows, key=lambda r: r["n"], reverse=True,
        )
    except Exception as exc:
        out["agg_error"] = repr(exc)

    return out
