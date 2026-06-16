"""v5.6.0 — proxy-injected tools subsystem.

Operator ask 2026-06-14: "some bots have said they can't do excel docs —
can we insert tools to do this for them?"

This subsystem appends a small set of helper tools (e.g.
``read_xlsx_to_markdown``) to every ``/v1/messages`` request before
forwarding to the upstream model. When the model responds with a
``tool_use`` block for one of our tools, the request handler runs the
tool in-process and re-issues the conversation with the
``tool_result`` injected so the model sees the file content. The
caller never sees the tool_use block; they get the final answer with
the file content already incorporated.

v5.6.0 limitations (will be lifted in v5.6.1):
- Non-streaming only. Streaming requests skip injection entirely so a
  proxy tool can never fire mid-stream and confuse the caller.
- Anthropic ``/v1/messages`` shape only. OpenAI ``/v1/chat/completions``
  uses a different tool envelope (``type: "function"``); that lands in
  v5.6.2.

Architecture:
- ``ProxyTool`` dataclass holds: ``name``, ``anthropic_schema``,
  ``openai_schema``, and a callable ``run`` coroutine.
- ``REGISTRY`` is the list of every proxy tool. Add a new tool by
  appending a ``ProxyTool`` instance.
- ``inject_anthropic(body)`` appends ``anthropic_schema`` from each
  registered tool to ``body["tools"]``, creating the list if absent.
  Returns the (possibly new) tools list.
- ``find_proxy_tool_use(response_content)`` scans the assistant's
  content blocks for a ``tool_use`` whose ``name`` is in our registry.
  Returns ``(tool, input_obj, tool_use_id)`` or ``None``.
- ``run_tool(tool, input_obj)`` executes the tool and returns a
  string suitable for the ``content`` field of a ``tool_result`` block.
- ``build_tool_result_message(tool_use_id, output)`` returns a
  ``user``-role message dict carrying the tool_result block.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional, Tuple


@dataclass
class ProxyTool:
    """One proxy-injected tool registration."""

    name: str
    anthropic_schema: dict
    openai_schema: dict
    run: Callable[[dict], Awaitable[str]]


def _build_registry() -> List[ProxyTool]:
    """Lazy-build the registry so importing this module doesn't pull
    in heavy deps (openpyxl) for unrelated code paths."""
    from app.proxy_tools.excel import EXCEL_TOOL
    return [EXCEL_TOOL]


# Module-level lazy cache so callers don't rebuild on every request.
_REGISTRY_CACHE: Optional[List[ProxyTool]] = None


def get_registry() -> List[ProxyTool]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = _build_registry()
    return _REGISTRY_CACHE


async def get_registry_async() -> List[ProxyTool]:
    """v5.7.1 — async variant that ALSO sources from the FastMCP
    aggregator (via the bridge). Use this from the /v1/messages
    handler. Sync ``get_registry()`` remains for any test/diagnostic
    paths that don't have an event loop handy.

    Tools are deduplicated by name — if the same tool is registered
    both as a static ProxyTool and via the MCP bridge (e.g.
    ``read_xlsx_to_markdown`` is in both), the static one wins so
    we don't accidentally double-inject the schema.
    """
    static = get_registry()
    names = {t.name for t in static}
    try:
        from app.proxy_tools.mcp_bridge import get_bridge_proxy_tools
        bridge = await get_bridge_proxy_tools()
    except Exception:
        bridge = []
    out = list(static)
    for t in bridge:
        if t.name not in names:
            out.append(t)
            names.add(t.name)
    return out


def inject_anthropic(body: dict) -> List[dict]:
    """Append every registered tool's Anthropic schema to
    ``body["tools"]``. Idempotent — if a tool with the same ``name``
    is already in the list (e.g. the caller passed their own
    ``read_xlsx_to_markdown``), we don't re-add. Returns the new
    tools list (also assigned back to ``body``).

    v5.7.1: sync — only sees static tools. Use ``inject_anthropic_async``
    for the bridge-aware variant from the /v1/messages handler.
    """
    existing = body.get("tools") or []
    existing_names = {
        t.get("name") for t in existing if isinstance(t, dict)
    }
    for proxy_tool in get_registry():
        if proxy_tool.name not in existing_names:
            existing = list(existing) + [proxy_tool.anthropic_schema]
    body["tools"] = existing
    return existing


async def inject_anthropic_async(body: dict) -> List[dict]:
    """v5.7.1 — async injection that sources from BOTH the static
    registry AND the FastMCP aggregator bridge. The /v1/messages
    handler should use this so every MCP-registered tool (markitdown,
    aggregator sub-server tools, future capability-scout suggestions)
    is automatically available to the model with zero code change
    per tool.

    Idempotency rules carry over from ``inject_anthropic``: if the
    caller passed a tool with the same ``name`` we don't re-add.
    """
    existing = body.get("tools") or []
    existing_names = {
        t.get("name") for t in existing if isinstance(t, dict)
    }
    for proxy_tool in await get_registry_async():
        if proxy_tool.name not in existing_names:
            existing = list(existing) + [proxy_tool.anthropic_schema]
            existing_names.add(proxy_tool.name)
    body["tools"] = existing
    return existing


async def find_proxy_tool_use_async(
    content_blocks: List[dict],
) -> Optional[Tuple[ProxyTool, dict, str]]:
    """v5.7.1 — async variant of ``find_proxy_tool_use`` that ALSO
    matches against the FastMCP aggregator bridge tools. Returns the
    first ``(tool, input_obj, tool_use_id)`` whose tool name matches
    any registered (static or bridge) ProxyTool."""
    if not isinstance(content_blocks, list):
        return None
    registry = await get_registry_async()
    by_name = {t.name: t for t in registry}
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if name in by_name:
            return (by_name[name], block.get("input") or {}, block.get("id") or "")
    return None


def find_proxy_tool_use(
    content_blocks: List[dict],
) -> Optional[Tuple[ProxyTool, dict, str]]:
    """Scan an assistant turn's content for a ``tool_use`` referencing
    a registered proxy tool.

    Returns the matched ``(ProxyTool, input_obj, tool_use_id)`` triple,
    or ``None``. If multiple proxy-tool uses are present in the same
    turn, returns the first; v5.6.1 streaming-aware handler will
    iterate over all of them.
    """
    if not isinstance(content_blocks, list):
        return None
    by_name = {t.name: t for t in get_registry()}
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if name in by_name:
            return (by_name[name], block.get("input") or {}, block.get("id") or "")
    return None


async def run_tool(tool: ProxyTool, input_obj: dict) -> str:
    """Run the tool. Exceptions are wrapped as a string so the model
    sees a useful error message rather than the proxy 5xx'ing."""
    try:
        return await tool.run(input_obj)
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def build_tool_result_message(tool_use_id: str, output: str) -> dict:
    """Construct the user-role message carrying a tool_result block.

    Anthropic's tool-use spec: after an assistant ``tool_use``, the
    conversation appends a ``user`` message whose ``content`` contains
    a ``tool_result`` block referencing the original ``tool_use_id``.
    The model then resumes generation in a follow-up assistant turn.
    """
    return {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": output,
        }],
    }


async def patch_inbound_tool_results(messages_list: list) -> int:
    """v5.6.1 — server-side patcher for streaming round-trips.

    Scan ``messages_list`` for ``user``-role messages whose ``content``
    contains ``tool_result`` blocks. For each tool_result, find the
    preceding assistant turn's matching ``tool_use`` (by ``tool_use_id``).
    If the tool_use's ``name`` refers to a proxy-injected tool, EXECUTE
    the tool server-side with the original ``input`` and REPLACE the
    tool_result's ``content`` with the actual output.

    This is how streaming tool-use works in v5.6.1: the streaming
    client receives a ``tool_use`` block it can't execute, sends a
    follow-up ``/v1/messages`` with a PLACEHOLDER ``tool_result``, and
    the proxy fills in the real content before the upstream model
    sees the message. The client never needs to implement the tool.

    Idempotent: if the tool_result content is already non-placeholder
    (i.e. the caller already executed the tool themselves), we don't
    overwrite. Heuristic: only patch when the existing content is
    empty / whitespace / a known placeholder marker.

    Returns the number of tool_result blocks patched.
    """
    if not isinstance(messages_list, list) or len(messages_list) < 2:
        return 0
    registry = await get_registry_async()
    by_name = {t.name: t for t in registry}
    # Build a lookup: tool_use_id → (proxy_tool, input_obj)
    tool_uses: dict[str, tuple[ProxyTool, dict]] = {}
    for msg in messages_list:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name in by_name:
                tool_uses[block.get("id") or ""] = (
                    by_name[name],
                    block.get("input") or {},
                )
    if not tool_uses:
        return 0
    patched = 0
    for msg in messages_list:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tuid = block.get("tool_use_id") or ""
            match = tool_uses.get(tuid)
            if not match:
                continue
            existing = block.get("content")
            # Idempotency heuristic: only patch placeholders.
            if isinstance(existing, str) and existing.strip() and existing.strip() not in (
                "PLACEHOLDER", "pending", "TODO", "...", "n/a",
            ):
                # Already has a real result — caller executed the tool.
                continue
            proxy_tool, input_obj = match
            try:
                output = await run_tool(proxy_tool, input_obj)
                block["content"] = output
                patched += 1
            except Exception:
                # Leave the block alone on error — caller-supplied
                # content survives.
                pass
    return patched
