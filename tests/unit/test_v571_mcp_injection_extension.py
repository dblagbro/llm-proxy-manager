"""v5.7.1 — Extend v5.6.0 injection to all MCP tools + markitdown +
system-prompt augmentation.

Operator-approved 2026-06-14. This ship delivers:
1. markitdown registered as an MCP tool — kills the biggest
   "I can't read DOCX/PDF/PPTX/HTML/EPUB" failure bucket.
2. `proxy_tools/mcp_bridge.py` — surfaces every FastMCP aggregator
   tool to /v1/messages injection. Future tools registered on /mcp
   are automatically available with zero code change.
3. `inject_anthropic_async` + `find_proxy_tool_use_async` — async
   variants used by the /v1/messages handler (the sync ones remain
   for diagnostics and tests).
4. `api_keys.system_prompt_mcp_augmentation` BOOLEAN column.
   Per-key opt-in nudge prepended to body["system"] telling the model
   to call tools instead of refusing.

Tests cover: registration shape, idempotent dedup, source-grep
contracts in messages.py, schema column presence + ALTER, bridge
behavior with a mocked FastMCP root.
"""
from __future__ import annotations

import asyncio
import base64
import io
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ──── Deps + ALTER + ORM column ────────────────────────────────────


def test_markitdown_in_requirements():
    src = Path("requirements.txt").read_text()
    assert "markitdown" in src


def test_alter_adds_system_prompt_mcp_augmentation():
    """ALTER must run on upgrades; without it the model attribute is
    Always-None for existing keys and the augmentation never fires."""
    src = Path("app/models/database.py").read_text()
    assert (
        "ALTER TABLE api_keys ADD COLUMN system_prompt_mcp_augmentation BOOLEAN DEFAULT 0"
        in src
    )


def test_apikey_model_has_augmentation_column():
    from app.models.db import ApiKey
    cols = {c.name for c in ApiKey.__table__.columns}
    assert "system_prompt_mcp_augmentation" in cols


# ──── MCP tools ────────────────────────────────────────────────────


def test_convert_document_tool_registered():
    src = Path("app/mcp_server/server.py").read_text()
    assert 'name="convert_document_to_markdown"' in src


@pytest.mark.asyncio
async def test_convert_document_runs_against_simple_html():
    """End-to-end through markitdown for an HTML payload — proves the
    wrapper is wired correctly."""
    from app.mcp_server.tools import convert_document_to_markdown
    html = b"<html><body><h1>Hello</h1><p>World</p></body></html>"
    out = await convert_document_to_markdown(
        file_b64=base64.b64encode(html).decode(),
        file_extension="html",
    )
    assert "Hello" in out
    assert "World" in out
    # markdown shape — at least one of these rendering artifacts
    assert any(token in out for token in ("# ", "## ", "Hello\n"))


# ──── Bridge ──────────────────────────────────────────────────────


def test_bridge_module_exists_with_get_function():
    from app.proxy_tools.mcp_bridge import get_bridge_proxy_tools
    assert callable(get_bridge_proxy_tools)


@pytest.mark.asyncio
async def test_bridge_returns_empty_when_mcp_app_not_mounted(monkeypatch):
    """If the FastAPI lifespan failed to mount /mcp (graceful boot
    degradation), the bridge must return [] instead of raising — so
    the /v1/messages injection still works for static tools."""
    from app.proxy_tools.mcp_bridge import (
        get_bridge_proxy_tools, invalidate_cache,
    )
    invalidate_cache()
    # Patch _mcp_sub_app to None
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "_mcp_sub_app", None, raising=False)
    out = await get_bridge_proxy_tools()
    assert out == []


@pytest.mark.asyncio
async def test_bridge_converts_mcp_tools_to_proxy_tools(monkeypatch):
    """Mock the FastMCP root's list_tools to return 2 tools; bridge
    should produce 2 ProxyTool instances with matching names + shape."""
    from app.proxy_tools.mcp_bridge import (
        get_bridge_proxy_tools, invalidate_cache,
    )
    invalidate_cache()

    @dataclass
    class FakeMCPTool:
        name: str
        description: str
        inputSchema: dict

    fake_mcp = MagicMock()
    fake_mcp.list_tools = AsyncMock(return_value=[
        FakeMCPTool(name="alpha", description="A.", inputSchema={"type": "object"}),
        FakeMCPTool(name="beta",  description="B.", inputSchema={"type": "object", "properties": {"x": {"type": "string"}}}),
    ])
    fake_sub_app = MagicMock()
    fake_sub_app.state.mcp = fake_mcp
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "_mcp_sub_app", fake_sub_app, raising=False)
    out = await get_bridge_proxy_tools()
    names = {t.name for t in out}
    assert names == {"alpha", "beta"}
    # Each schema must be Anthropic-shape
    for pt in out:
        s = pt.anthropic_schema
        assert s["name"] == pt.name
        assert "input_schema" in s


# ──── Async registry ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_registry_combines_static_and_bridge(monkeypatch):
    """get_registry_async must surface static (v5.6.0 Excel) PLUS
    bridge tools, deduped by name (static wins)."""
    from app.proxy_tools import get_registry_async
    from app.proxy_tools.mcp_bridge import invalidate_cache
    invalidate_cache()

    # Bridge returns one new tool + one that collides with static
    async def fake_bridge():
        from app.proxy_tools import ProxyTool

        async def _r(_i):
            return ""
        return [
            ProxyTool(name="read_xlsx_to_markdown",
                      anthropic_schema={"name": "read_xlsx_to_markdown"},
                      openai_schema={}, run=_r),
            ProxyTool(name="convert_document_to_markdown",
                      anthropic_schema={"name": "convert_document_to_markdown"},
                      openai_schema={}, run=_r),
        ]
    monkeypatch.setattr(
        "app.proxy_tools.mcp_bridge.get_bridge_proxy_tools",
        fake_bridge,
    )
    reg = await get_registry_async()
    names = [t.name for t in reg]
    # Static Excel + bridge markitdown; bridge's duplicate Excel dropped
    assert "read_xlsx_to_markdown" in names
    assert "convert_document_to_markdown" in names
    assert names.count("read_xlsx_to_markdown") == 1


# ──── messages.py wiring ──────────────────────────────────────────


def test_messages_handler_uses_async_inject():
    """v5.7.1 — the non-streaming /v1/messages handler must call
    ``inject_anthropic_async`` (so MCP-bridge tools land in body.tools)
    instead of the v5.6.0 sync ``inject_anthropic``."""
    src = Path("app/api/messages.py").read_text()
    assert "from app.proxy_tools import inject_anthropic_async" in src
    assert "await inject_anthropic_async(body)" in src


def test_messages_handler_uses_async_find():
    """Response interception must use ``find_proxy_tool_use_async`` so
    a bridge-tool tool_use is detected (the sync variant only sees
    static-registry tools)."""
    src = Path("app/api/messages.py").read_text()
    assert "find_proxy_tool_use_async" in src
    assert "await find_proxy_tool_use_async(" in src


def test_system_prompt_augmentation_gated_by_key_flag():
    """Augmentation must check ``key_record.system_prompt_mcp_augmentation``
    AND require that tool injection actually happened. Pin both."""
    src = Path("app/api/messages.py").read_text()
    # Both conditions in one `if`
    assert "_proxy_tools_injected and getattr(key_record, \"system_prompt_mcp_augmentation\", False)" in src
    # The nudge must mention the specific file formats (so a tester can
    # confirm intent by reading the string)
    assert "Excel" in src and "PDF" in src and "PowerPoint" in src


def test_system_prompt_augmentation_handles_list_and_str_shapes():
    """Anthropic accepts body['system'] as either a string OR a list
    of {type:text, text:...} blocks. Augmentation must work on both."""
    src = Path("app/api/messages.py").read_text()
    # Source-grep both branches
    assert 'isinstance(existing_system, str)' in src
    assert 'isinstance(existing_system, list)' in src
