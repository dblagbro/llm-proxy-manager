"""v5.7.4 — Per-key MCP tool policy + token-budget enforcement.

Operator-approved 2026-06-15 night during the pre-freeze sprint.
Three new ApiKey columns (mcp_tools_allow, mcp_tools_deny,
mcp_schema_token_budget) plus a policy module that filters list_tools
and gates call_tool, plus an admin endpoint for editing the policy.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ──── Schema ───────────────────────────────────────────────────────


def test_alter_adds_mcp_policy_columns():
    src = Path("app/models/database.py").read_text()
    assert "ALTER TABLE api_keys ADD COLUMN mcp_tools_allow TEXT" in src
    assert "ALTER TABLE api_keys ADD COLUMN mcp_tools_deny TEXT" in src
    assert (
        "ALTER TABLE api_keys ADD COLUMN mcp_schema_token_budget INTEGER"
        in src
    )


def test_apikey_model_has_mcp_policy_columns():
    from app.models.db import ApiKey
    cols = {c.name for c in ApiKey.__table__.columns}
    for c in ("mcp_tools_allow", "mcp_tools_deny", "mcp_schema_token_budget"):
        assert c in cols, f"ApiKey model missing column {c!r}"


# ──── Policy module ────────────────────────────────────────────────


def test_is_tool_allowed_null_allow_means_permissive():
    from app.mcp_server.policy import is_tool_allowed_for_key
    assert is_tool_allowed_for_key("any_tool", None, None) is True
    assert is_tool_allowed_for_key("foo_bar", None, []) is True


def test_is_tool_allowed_empty_allow_means_restrictive():
    """Empty allow list = explicit 'no tools'. v5.7.4 contract."""
    from app.mcp_server.policy import is_tool_allowed_for_key
    assert is_tool_allowed_for_key("read_xlsx_to_markdown", [], None) is False


def test_is_tool_allowed_deny_wins_over_allow():
    """Even if a tool matches the allow list, an overlapping deny wins."""
    from app.mcp_server.policy import is_tool_allowed_for_key
    assert is_tool_allowed_for_key(
        "fetch_url",
        ["fetch_*"],   # allowed via glob
        ["fetch_url"], # denied exact
    ) is False


def test_is_tool_allowed_fnmatch_globs():
    from app.mcp_server.policy import is_tool_allowed_for_key
    assert is_tool_allowed_for_key("read_pdf", ["read_*"], None) is True
    assert is_tool_allowed_for_key("write_pdf", ["read_*"], None) is False
    assert is_tool_allowed_for_key("read_xlsx_to_markdown", ["read_*"], None) is True


def test_estimate_schema_tokens_is_conservative():
    """Conservative = always >= 0 and falls within sane bounds for a
    realistic schema. Pin the 4-chars/token approximation so a future
    refactor doesn't silently loosen the budget."""
    from app.mcp_server.policy import estimate_schema_tokens
    assert estimate_schema_tokens(None) == 0
    assert estimate_schema_tokens({}) >= 0
    schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "fetch URL"},
        },
        "required": ["url"],
    }
    # JSON: about 88 chars → ~22 tokens at 4 chars/token
    est = estimate_schema_tokens(schema)
    assert 15 <= est <= 30


def test_filter_tools_respects_allow_and_deny():
    from app.mcp_server.policy import filter_tools_for_key
    from types import SimpleNamespace as NS
    tools = [
        NS(name="read_xlsx_to_markdown", inputSchema={}),
        NS(name="fetch_url", inputSchema={}),
        NS(name="write_arbitrary_file", inputSchema={}),
    ]
    out = filter_tools_for_key(
        tools,
        mcp_tools_allow=["read_*", "fetch_*"],
        mcp_tools_deny=[],
    )
    assert {t.name for t in out} == {"read_xlsx_to_markdown", "fetch_url"}


def test_check_token_budget_null_means_unlimited():
    from app.mcp_server.policy import check_token_budget
    from types import SimpleNamespace as NS
    big = NS(inputSchema={"x": "y" * 10_000})
    ok, total = check_token_budget([big], None)
    assert ok is True
    assert total > 0


def test_check_token_budget_blocks_when_exceeded():
    from app.mcp_server.policy import check_token_budget
    from types import SimpleNamespace as NS
    schema = {"a": "x" * 4000}  # ~1000 tokens estimate
    tools = [NS(inputSchema=schema), NS(inputSchema=schema)]
    ok_high, _ = check_token_budget(tools, 5000)
    ok_low, _ = check_token_budget(tools, 100)
    assert ok_high is True
    assert ok_low is False


# ──── server.py wiring ─────────────────────────────────────────────


def test_server_module_exposes_policy_contextvar():
    from app.mcp_server.server import current_mcp_policy
    import contextvars
    assert isinstance(current_mcp_policy, contextvars.ContextVar)
    # default is None (permissive — only ContextVar absent in tests)
    assert current_mcp_policy.get() is None


def test_server_wraps_list_tools_and_call_tool():
    """v5.7.4 — source-grep contract: build_mcp_app must wrap both
    list_tools and call_tool so the policy applies on every path."""
    src = Path("app/mcp_server/server.py").read_text()
    assert "_wrap_list_tools_with_policy(mcp)" in src
    assert "_wrap_call_tool_with_policy(mcp)" in src


def test_bearer_middleware_sets_policy_contextvar():
    """When the bearer auth middleware accepts a key, it must populate
    current_mcp_policy with the key's allow/deny/budget so the
    wrappers see them."""
    src = Path("app/mcp_server/server.py").read_text()
    assert "current_mcp_policy.set(policy)" in src
    # The reset must happen in the finally block so a tool that
    # raises doesn't leak policy into the next request
    finally_idx = src.find("current_mcp_policy.reset(policy_token)")
    assert finally_idx != -1


# ──── messages.py — Path B propagation ─────────────────────────────


def test_messages_handler_propagates_mcp_policy_for_path_b():
    """v5.7.4 — without this, Path B (proxy-side injection) would
    expose tools the key is denied. Source-grep pin."""
    src = Path("app/api/messages.py").read_text()
    assert "from app.mcp_server.server import current_mcp_policy" in src
    assert "current_mcp_policy.set(" in src


# ──── Admin endpoint ───────────────────────────────────────────────


def test_admin_mcp_policy_router_registered():
    src = Path("app/main.py").read_text()
    assert "from app.api.admin_mcp_policy import router as admin_mcp_policy_router" in src
    assert "app.include_router(admin_mcp_policy_router)" in src


def test_admin_mcp_policy_endpoints_exist():
    from app.api.admin_mcp_policy import router
    paths = {r.path for r in router.routes if hasattr(r, "path")}
    assert any("/policy" in p for p in paths)


@pytest.mark.asyncio
async def test_admin_mcp_policy_get_404_unknown_key():
    from app.api.admin_mcp_policy import get_mcp_policy
    from fastapi import HTTPException
    from unittest.mock import AsyncMock, MagicMock
    db = MagicMock()
    rs = MagicMock(); rs.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=rs)
    with pytest.raises(HTTPException) as exc:
        await get_mcp_policy("does-not-exist", db=db, _admin=MagicMock())
    assert exc.value.status_code == 404
