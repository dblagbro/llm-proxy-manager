"""v5.7.5 — Frontend MCP dashboard + /api/admin/mcp/summary endpoint."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_admin_mcp_summary_router_registered():
    src = Path("app/main.py").read_text()
    assert "from app.api.admin_mcp_summary import router as admin_mcp_summary_router" in src
    assert "app.include_router(admin_mcp_summary_router)" in src


def test_admin_mcp_summary_endpoint_exists():
    from app.api.admin_mcp_summary import router
    paths = {r.path for r in router.routes if hasattr(r, "path")}
    assert any("/summary" in p for p in paths)


@pytest.mark.asyncio
async def test_admin_mcp_summary_returns_shape_on_empty_db():
    """With no mcp_tool_calls and no FastMCP root mounted, the
    endpoint still returns a well-shaped response (no 500). Defensive
    aggregator design."""
    from app.api.admin_mcp_summary import mcp_summary
    db = MagicMock()

    # Mock the SQL aggregations to return empty/zero
    row_count = MagicMock(); row_count.first.return_value = (0, 0)
    empty_rs = MagicMock(); empty_rs.fetchall.return_value = []
    db.execute = AsyncMock(side_effect=[row_count, empty_rs, empty_rs, empty_rs])

    out = await mcp_summary(db=db, _admin=MagicMock())
    assert "tools_live" in out
    assert "calls_by_tool_24h" in out
    assert "calls_by_key_24h" in out
    assert "latency_by_tool_24h" in out
    assert out["total_calls_24h"] == 0
    assert out["total_errors_24h"] == 0


def test_mcp_page_component_exists():
    p = Path("frontend/src/pages/McpPage.tsx")
    assert p.is_file()
    src = p.read_text()
    # Anchors that prove the data shape is being consumed
    assert "calls_by_tool_24h" in src
    assert "tools_live" in src
    assert "/api/admin/mcp/summary" in src


def test_mcp_route_registered_in_app():
    src = Path("frontend/src/App.tsx").read_text()
    assert "import { McpPage }" in src
    assert "<McpPage />" in src
    assert "admin/mcp" in src


def test_mcp_nav_link_in_sidebar():
    src = Path("frontend/src/components/layout/Sidebar.tsx").read_text()
    assert "/admin/mcp" in src
    # Admin-only: must include the hidden guard
    assert "hidden: !isAdmin" in src
