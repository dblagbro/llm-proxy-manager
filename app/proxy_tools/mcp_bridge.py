"""v5.7.1 — Bridge that surfaces FastMCP aggregator tools to the
``/v1/messages`` tool-injection mechanism.

Extends v5.6.0's static-registry pattern: when a non-streaming
``/v1/messages`` request hits the proxy, the existing
``inject_anthropic`` function reads from ``get_registry()`` and
appends each tool's Anthropic schema to ``body["tools"]``. v5.6.0
shipped one static tool (``read_xlsx_to_markdown``); v5.7.1 makes
``get_registry()`` ALSO source from the MCP aggregator, so every
tool registered on the FastMCP root is automatically available to
the model in /v1/messages requests — no code change per new tool.

Caches the tool list for 60 seconds. Live-aggregator calls to
``list_tools`` are fast (<5ms for in-process tools) but cluster sync
+ many concurrent /v1/messages requests would otherwise hit the
aggregator on every request.

Routing tool_use back: ``find_proxy_tool_use`` matches by name
(unchanged from v5.6.0); when a bridge ProxyTool's ``.run()`` is
called, it invokes ``mcp.call_tool(name, input_obj)`` on the
FastMCP root. The MCP sub-server runs the tool and the proxy returns
the result to the outer messages handler, which builds a
``tool_result`` block and re-issues the conversation.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from app.proxy_tools import ProxyTool

logger = logging.getLogger(__name__)


_CACHE_TTL_SEC = 60.0
_cache: dict[str, Any] = {"ts": 0.0, "tools": []}


def _convert_mcp_tool_to_anthropic_schema(mcp_tool: Any) -> dict:
    """MCP Tool → Anthropic-shape tool schema.

    MCP's tool object exposes ``name``, ``description``, and
    ``inputSchema`` (JSON Schema). Anthropic's /v1/messages expects
    ``{name, description, input_schema}`` — same shape, just
    differently-named keys.
    """
    return {
        "name": getattr(mcp_tool, "name", ""),
        "description": getattr(mcp_tool, "description", "") or "",
        "input_schema": getattr(mcp_tool, "inputSchema", None) or {"type": "object"},
    }


async def _list_mcp_tools_cached() -> list[Any]:
    """Get the live tool list from the FastMCP root, 60s TTL cache.

    Returns an empty list if the FastMCP root isn't mounted yet (e.g.
    during startup before the lifespan enters). The injection
    mechanism is no-op-safe when the registry is empty.
    """
    now = time.time()
    if _cache["tools"] and (now - _cache["ts"]) < _CACHE_TTL_SEC:
        return _cache["tools"]
    try:
        from app.main import _mcp_sub_app  # type: ignore
        if _mcp_sub_app is None:
            return []
        mcp = _mcp_sub_app.state.mcp
        tools = await mcp.list_tools()
    except Exception as exc:
        logger.warning("mcp_bridge.list_tools_failed err=%s", exc)
        return []
    _cache["tools"] = tools
    _cache["ts"] = now
    return tools


def invalidate_cache() -> None:
    """For tests + admin endpoints. Forces next call to re-list."""
    _cache["ts"] = 0.0
    _cache["tools"] = []


async def _run_via_mcp(tool_name: str, input_obj: dict) -> str:
    """Execute a tool via the FastMCP root's ``call_tool``. Catches
    everything and returns ``error: <reason>`` because the outer
    tool-injection loop expects a string."""
    try:
        from app.main import _mcp_sub_app  # type: ignore
        if _mcp_sub_app is None:
            return "error: mcp aggregator not running"
        mcp = _mcp_sub_app.state.mcp
        result = await mcp.call_tool(tool_name, input_obj)
        # FastMCP.call_tool returns ``(structured_content, content_list)``;
        # the content_list is a list of TextContent / ImageContent.
        # For Anthropic-shape tool_result.content, we flatten text.
        try:
            structured, content = result
        except Exception:
            structured, content = None, result
        if isinstance(content, list):
            parts: list[str] = []
            for c in content:
                txt = getattr(c, "text", None)
                if isinstance(txt, str):
                    parts.append(txt)
            if parts:
                return "\n".join(parts)
        if isinstance(content, str):
            return content
        if structured is not None:
            return str(structured)
        return str(content) if content is not None else ""
    except Exception as exc:
        logger.warning("mcp_bridge.call_tool_failed tool=%s err=%s", tool_name, exc)
        return f"error: {type(exc).__name__}: {exc}"


async def get_bridge_proxy_tools() -> list[ProxyTool]:
    """Materialize a ProxyTool for every tool currently registered on
    the FastMCP root. Each ProxyTool's ``.run()`` round-trips through
    the aggregator's ``call_tool``.

    This is async because ``list_tools`` is async on the FastMCP
    side. v5.6.0's ``inject_anthropic`` is sync — we'll add an async
    counterpart in __init__.py that uses this.
    """
    mcp_tools = await _list_mcp_tools_cached()
    out: list[ProxyTool] = []
    for mt in mcp_tools:
        name = getattr(mt, "name", "")
        if not name:
            continue
        anthropic_schema = _convert_mcp_tool_to_anthropic_schema(mt)

        async def _runner(input_obj: dict, _name=name) -> str:
            return await _run_via_mcp(_name, input_obj)

        out.append(ProxyTool(
            name=name,
            anthropic_schema=anthropic_schema,
            openai_schema={},  # v5.6.2 will fill when /v1/chat/completions lands
            run=_runner,
        ))
    return out
