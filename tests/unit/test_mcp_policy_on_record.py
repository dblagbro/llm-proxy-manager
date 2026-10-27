"""Regression: per-key MCP tool policy must be carried onto ApiKeyRecord.

Before v5.22.43 the ORM had mcp_tools_allow / mcp_tools_deny /
mcp_schema_token_budget columns and the admin UI set them, but verify_api_key
never copied them onto the record, so every enforcement site read
getattr(record, "mcp_tools_allow", None) == None → allow-all. That voided both
the allow/deny lists and the documented allow=[] "disable MCP" opt-out.
"""
from __future__ import annotations

from app.auth.keys import ApiKeyRecord
from app.mcp_server.policy import is_tool_allowed_for_key


def test_record_has_mcp_policy_fields():
    r = ApiKeyRecord(id="k1", name="n", key_type="standard")
    # Fields exist and default to the permissive/None baseline.
    assert hasattr(r, "mcp_tools_allow")
    assert hasattr(r, "mcp_tools_deny")
    assert hasattr(r, "mcp_schema_token_budget")
    assert r.mcp_tools_allow is None
    assert r.mcp_tools_deny is None


def test_empty_allow_list_disables_all_tools():
    # allow=[] is the "disable MCP entirely" opt-out; it must deny every tool.
    r = ApiKeyRecord(id="k2", name="n", key_type="standard", mcp_tools_allow=[])
    policy_allow = r.mcp_tools_allow
    assert policy_allow == []  # preserved, NOT coerced to None
    assert is_tool_allowed_for_key("fetch_url", policy_allow, r.mcp_tools_deny) is False
    assert is_tool_allowed_for_key("read_file", policy_allow, r.mcp_tools_deny) is False


def test_allow_deny_lists_enforced():
    r = ApiKeyRecord(
        id="k3",
        name="n",
        key_type="standard",
        mcp_tools_allow=["read_*"],
        mcp_tools_deny=["read_secret"],
    )
    assert is_tool_allowed_for_key("read_file", r.mcp_tools_allow, r.mcp_tools_deny) is True
    assert is_tool_allowed_for_key("fetch_url", r.mcp_tools_allow, r.mcp_tools_deny) is False
    # deny wins over allow
    assert is_tool_allowed_for_key("read_secret", r.mcp_tools_allow, r.mcp_tools_deny) is False
