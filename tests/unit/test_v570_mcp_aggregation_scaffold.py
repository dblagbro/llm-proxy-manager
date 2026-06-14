"""v5.7.0 — MCP aggregation endpoint scaffold pin tests.

Operator-approved 2026-06-14 priority decision: ship the MCP project
on the v5.7.x ladder. v5.7.0 = aggregation endpoint + 2 in-process
tools (read_xlsx_to_markdown, fetch_url). v5.7.1 adds external stdio
sub-servers via FastMCP mount().

Pin contracts:
1. mcp SDK in requirements.
2. `app/mcp_server/` package exists with the 3 expected modules.
3. McpToolCall ORM model registered.
4. FastMCP root constructed with the right production settings
   (stateless_http=True, json_response=True, streamable_http_path="/").
5. Bearer-key auth middleware on the sub-app.
6. main.py mounts /mcp + runs session_manager in the lifespan.
7. read_xlsx_to_markdown reuses v5.6.0's renderer (no code drift
   between the two surfaces).
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ──── Dependency + module layout ────────────────────────────────────


def test_mcp_in_requirements():
    src = Path("requirements.txt").read_text()
    assert "mcp>=1.27,<2" in src


def test_mcp_server_package_exists():
    assert Path("app/mcp_server/__init__.py").is_file()
    assert Path("app/mcp_server/server.py").is_file()
    assert Path("app/mcp_server/tools.py").is_file()


def test_mcp_tool_call_model_registered():
    """McpToolCall must be importable from the canonical db module so
    Base.metadata picks it up at init_db."""
    from app.models.db import McpToolCall
    assert McpToolCall.__tablename__ == "mcp_tool_calls"
    cols = {c.name for c in McpToolCall.__table__.columns}
    assert {
        "id", "created_at", "api_key_id", "parent_request_id",
        "tool_name", "mcp_server_id", "input_summary",
        "output_bytes", "latency_ms", "ok", "error_msg",
    }.issubset(cols)


# ──── FastMCP root construction ────────────────────────────────────


def test_build_mcp_app_uses_production_settings():
    """stateless_http=True + json_response=True + streamable_http_path='/'
    is the production combo (no sticky sessions, scales horizontally,
    avoids SDK issue #1367)."""
    src = Path("app/mcp_server/server.py").read_text()
    assert 'stateless_http=True' in src
    assert 'json_response=True' in src
    assert 'streamable_http_path="/"' in src


def test_build_mcp_app_returns_starlette_sub_app():
    from app.mcp_server.server import build_mcp_app
    sub_app = build_mcp_app()
    # The Starlette sub-app should have the FastMCP instance stashed
    # on its state for the lifespan integration.
    assert hasattr(sub_app, "state")
    assert hasattr(sub_app.state, "mcp")


def test_bearer_auth_middleware_registered():
    """Auth middleware must be added so unauth'd requests can't reach
    tools. Source-grep is the simplest contract."""
    src = Path("app/mcp_server/server.py").read_text()
    assert "BearerKeyAuthMiddleware" in src
    assert "add_middleware(BearerKeyAuthMiddleware)" in src


def test_two_in_process_tools_registered():
    """v5.7.0 ships read_xlsx_to_markdown + fetch_url. v5.7.1 adds
    sub-servers. Source-grep pins that we registered exactly these
    two and didn't accidentally add list_directory or a shell tool."""
    src = Path("app/mcp_server/server.py").read_text()
    assert 'name="read_xlsx_to_markdown"' in src
    assert 'name="fetch_url"' in src
    # Explicit anti-pin: nothing dangerous shipped in scaffold
    assert "list_directory" not in src
    assert "run_shell" not in src
    assert "execute_command" not in src


# ──── main.py integration ──────────────────────────────────────────


def test_main_py_mounts_mcp():
    src = Path("app/main.py").read_text()
    assert "from app.mcp_server.server import build_mcp_app" in src
    assert 'app.mount("/mcp", _mcp_sub_app)' in src


def test_main_py_lifespan_runs_session_manager():
    """SDK issue #1367 — without session_manager.run() entered in the
    lifespan, the first request raises 'Task group not initialized'."""
    src = Path("app/main.py").read_text()
    assert "_mcp_sub_app.state.mcp.session_manager.run()" in src
    # Belt-and-braces: the enter call must precede the yield
    enter_idx = src.find("_mcp_session_mgr_ctx.__aenter__()")
    yield_idx = src.find("\n    yield\n")
    assert enter_idx != -1 and yield_idx != -1
    assert enter_idx < yield_idx, (
        "session_manager must be entered BEFORE yield so the sub-app's "
        "task group is alive when the first request arrives"
    )


# ──── Bearer auth behavior ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_bearer_middleware_rejects_missing_header():
    from app.mcp_server.server import BearerKeyAuthMiddleware
    req = MagicMock()
    req.headers = {}  # no Authorization
    mw = BearerKeyAuthMiddleware(MagicMock())
    resp = await mw.dispatch(req, AsyncMock())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_middleware_rejects_malformed_header():
    from app.mcp_server.server import BearerKeyAuthMiddleware
    req = MagicMock()
    req.headers = {"authorization": "NotBearer 123"}
    mw = BearerKeyAuthMiddleware(MagicMock())
    resp = await mw.dispatch(req, AsyncMock())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_middleware_rejects_invalid_key():
    from app.mcp_server.server import BearerKeyAuthMiddleware
    req = MagicMock()
    req.headers = {"authorization": "Bearer some-invalid-key"}
    mw = BearerKeyAuthMiddleware(MagicMock())
    with patch(
        "app.auth.keys.verify_api_key",
        new=AsyncMock(side_effect=ValueError("invalid")),
    ):
        resp = await mw.dispatch(req, AsyncMock())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_middleware_sets_context_var_on_success():
    """Valid key → ContextVar set → tools attribute audit correctly."""
    from app.mcp_server.server import BearerKeyAuthMiddleware
    from app.mcp_server import current_api_key_id
    req = MagicMock()
    req.headers = {"authorization": "Bearer good-key"}
    mw = BearerKeyAuthMiddleware(MagicMock())

    fake_key = MagicMock(id="key-abc")
    captured: dict = {}

    async def fake_call_next(_r):
        captured["api_key_id"] = current_api_key_id.get()
        return MagicMock()

    with patch(
        "app.auth.keys.verify_api_key",
        new=AsyncMock(return_value=fake_key),
    ):
        await mw.dispatch(req, fake_call_next)

    assert captured["api_key_id"] == "key-abc"
    # ContextVar reset after dispatch
    assert current_api_key_id.get() is None


# ──── Tool implementations ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_xlsx_tool_reuses_v560_renderer():
    """v5.7.0 MCP wrapper must call the same _render_workbook as
    v5.6.0. Source-grep contract — guarantees no parallel-divergence
    when v5.6.1 ships streaming support and updates the renderer."""
    src = Path("app/mcp_server/tools.py").read_text()
    assert "from app.proxy_tools.excel import _fetch_bytes, _render_workbook" in src


@pytest.mark.asyncio
async def test_read_xlsx_tool_renders_b64():
    """End-to-end: feed a tiny xlsx through the MCP wrapper and
    verify it returns the markdown shape."""
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["product", "qty"])
    wb.active.append(["widget", 10])
    buf = io.BytesIO(); wb.save(buf)
    from app.mcp_server.tools import read_xlsx_to_markdown
    out = await read_xlsx_to_markdown(
        file_b64=base64.b64encode(buf.getvalue()).decode(),
    )
    assert "## Sheet: Sheet" in out
    assert "widget" in out


@pytest.mark.asyncio
async def test_fetch_url_rejects_non_http_schemes():
    from app.mcp_server.tools import fetch_url
    out = await fetch_url("file:///etc/passwd")
    assert out.startswith("error:")
    assert "must use http" in out


@pytest.mark.asyncio
async def test_fetch_url_rejects_empty_url():
    from app.mcp_server.tools import fetch_url
    out = await fetch_url("")
    assert out.startswith("error:")
