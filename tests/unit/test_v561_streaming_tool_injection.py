"""v5.6.1 — streaming /v1/messages tool injection + inbound tool_result patcher tests."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def test_streaming_gate_lifted_in_messages_handler():
    """Static-grep contract: the v5.6.0 ``if not stream:`` gate around
    proxy_tools injection MUST be lifted in v5.6.1 so streaming
    requests also get tool injection."""
    src = Path("app/api/messages.py").read_text()
    # The marker comment is non-load-bearing but identifies the lifted gate.
    assert "lifted in v5.6.1" in src
    assert "if True:  # was: if not stream" in src


def test_patch_inbound_tool_results_hook_wired():
    """Static-grep contract: the inbound tool_result patcher is called
    before upstream dispatch."""
    src = Path("app/api/messages.py").read_text()
    assert "from app.proxy_tools import patch_inbound_tool_results" in src
    assert "await patch_inbound_tool_results(body.get(\"messages\")" in src


def test_patch_inbound_function_exported():
    from app.proxy_tools import patch_inbound_tool_results
    assert callable(patch_inbound_tool_results)


@pytest.mark.asyncio
async def test_patch_inbound_replaces_placeholder_for_proxy_tool():
    """When messages contain ``assistant: tool_use(read_xlsx_to_markdown)``
    followed by ``user: tool_result(placeholder)``, the patcher executes
    the tool and replaces the placeholder content."""
    from app import proxy_tools

    # Mock get_registry_async to return a stub proxy tool
    class _StubTool:
        name = "read_xlsx_to_markdown"
    stub = _StubTool()

    monkeyed_run = AsyncMock(return_value="| col1 | col2 |\n|------|------|\n| 1 | 2 |")

    async def fake_registry():
        return [stub]

    async def fake_run(tool, input_obj):
        return await monkeyed_run(tool, input_obj)

    # Use real patch_inbound_tool_results but monkeypatch the registry
    # + run_tool. Save originals first.
    orig_registry = proxy_tools.get_registry_async
    orig_run = proxy_tools.run_tool
    try:
        proxy_tools.get_registry_async = fake_registry
        proxy_tools.run_tool = fake_run
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "summarise the file"}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_xyz",
                 "name": "read_xlsx_to_markdown",
                 "input": {"file_b64": "abc"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_xyz", "content": "PLACEHOLDER"},
            ]},
        ]
        n = await proxy_tools.patch_inbound_tool_results(messages)
        assert n == 1
        patched_content = messages[2]["content"][0]["content"]
        assert "col1" in patched_content
    finally:
        proxy_tools.get_registry_async = orig_registry
        proxy_tools.run_tool = orig_run


@pytest.mark.asyncio
async def test_patch_inbound_skips_when_caller_already_has_real_result():
    """If the tool_result content is already real (not a placeholder),
    we don't overwrite — caller did the work themselves."""
    from app import proxy_tools

    class _StubTool:
        name = "read_xlsx_to_markdown"
    stub = _StubTool()

    async def fake_registry():
        return [stub]
    async def fake_run(tool, input_obj):
        return "PROXY_EXECUTED"

    orig_registry = proxy_tools.get_registry_async
    orig_run = proxy_tools.run_tool
    try:
        proxy_tools.get_registry_async = fake_registry
        proxy_tools.run_tool = fake_run
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_xyz",
                 "name": "read_xlsx_to_markdown", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_xyz",
                 "content": "real markdown table from client"},
            ]},
        ]
        n = await proxy_tools.patch_inbound_tool_results(messages)
        assert n == 0
        assert messages[1]["content"][0]["content"] == "real markdown table from client"
    finally:
        proxy_tools.get_registry_async = orig_registry
        proxy_tools.run_tool = orig_run


@pytest.mark.asyncio
async def test_patch_inbound_no_op_when_no_proxy_tool_use():
    """tool_result for a non-proxy tool name is ignored — caller's
    own tool, not ours."""
    from app import proxy_tools

    async def fake_registry():
        return []
    orig_registry = proxy_tools.get_registry_async
    try:
        proxy_tools.get_registry_async = fake_registry
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1",
                 "name": "custom_caller_tool", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "PLACEHOLDER"},
            ]},
        ]
        n = await proxy_tools.patch_inbound_tool_results(messages)
        assert n == 0
    finally:
        proxy_tools.get_registry_async = orig_registry


@pytest.mark.asyncio
async def test_patch_inbound_handles_empty_messages():
    from app.proxy_tools import patch_inbound_tool_results
    assert await patch_inbound_tool_results([]) == 0
    assert await patch_inbound_tool_results(None) == 0
    assert await patch_inbound_tool_results([{"role": "user", "content": "hi"}]) == 0


@pytest.mark.asyncio
async def test_patch_inbound_handles_multiple_tool_uses():
    """Multiple proxy tool_use blocks → multiple patches."""
    from app import proxy_tools

    class _StubA:
        name = "read_xlsx_to_markdown"
    class _StubB:
        name = "fetch_url"

    async def fake_registry():
        return [_StubA(), _StubB()]

    async def fake_run(tool, input_obj):
        return f"output-of-{tool.name}"

    orig_registry = proxy_tools.get_registry_async
    orig_run = proxy_tools.run_tool
    try:
        proxy_tools.get_registry_async = fake_registry
        proxy_tools.run_tool = fake_run
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read_xlsx_to_markdown", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "fetch_url", "input": {"url": "x"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "PLACEHOLDER"},
                {"type": "tool_result", "tool_use_id": "t2", "content": ""},
            ]},
        ]
        n = await proxy_tools.patch_inbound_tool_results(messages)
        assert n == 2
        assert messages[1]["content"][0]["content"] == "output-of-read_xlsx_to_markdown"
        assert messages[1]["content"][1]["content"] == "output-of-fetch_url"
    finally:
        proxy_tools.get_registry_async = orig_registry
        proxy_tools.run_tool = orig_run
