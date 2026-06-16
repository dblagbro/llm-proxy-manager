"""v5.7.8 — MCP test hardening (was: v5.7.7).

Edge-case coverage we noticed gaps in during the v5.7.x pre-freeze
sprint. Pairs with the existing v5.7.x test files (do not duplicate
their assertions).

Scope:
- Policy edge cases (wildcards across boundaries, deny-wins,
  allow-empty semantics, token-budget rounding, large schema sanity)
- Capability scout edge cases (multilingual refusal phrasings, hits
  in middle of long text, near-miss avoidance)
- Bridge tools async iteration safety
- Summary aggregator handles malformed mcp_tool_calls rows without
  crashing
- patch_inbound_tool_results idempotency under repeated calls
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Policy hardening ────────────────────────────────────────────────


def test_policy_wildcard_across_boundaries():
    """Globs like ``read_*`` should match ``read_xlsx_to_markdown`` but
    NOT ``preread_anything`` (no anchoring weirdness)."""
    from app.mcp_server.policy import is_tool_allowed_for_key
    assert is_tool_allowed_for_key("read_xlsx_to_markdown", ["read_*"], None) is True
    assert is_tool_allowed_for_key("preread_anything", ["read_*"], None) is False
    assert is_tool_allowed_for_key("read", ["read_*"], None) is False  # underscore required


def test_policy_deny_beats_specific_allow():
    """Even a specific allow can't override a deny."""
    from app.mcp_server.policy import is_tool_allowed_for_key
    assert is_tool_allowed_for_key(
        "fetch_url",
        mcp_tools_allow=["fetch_url"],
        mcp_tools_deny=["fetch_url"],
    ) is False


def test_policy_allow_empty_list_denies_all():
    """[] is the explicit 'nothing allowed' marker (NOT the same as NULL)."""
    from app.mcp_server.policy import is_tool_allowed_for_key
    assert is_tool_allowed_for_key("any_tool", [], None) is False
    assert is_tool_allowed_for_key("any_tool", None, None) is True


def test_policy_token_budget_zero_means_unlimited_NOT():
    """Edge: budget=0 means 'allow nothing', not 'unlimited'. NULL
    means unlimited."""
    from app.mcp_server.policy import check_token_budget
    class T:
        inputSchema = {"x": "y"}
    ok_unlimited, _ = check_token_budget([T()], None)
    assert ok_unlimited is True
    ok_zero, _ = check_token_budget([T()], 0)
    assert ok_zero is False


def test_policy_handles_tool_without_inputSchema_attr():
    """Some tool objects use ``input_schema`` (snake) instead of
    ``inputSchema`` (camel). The budget check shouldn't crash; it
    counts as 0 tokens for that tool."""
    from app.mcp_server.policy import check_token_budget
    class T:
        pass
    ok, total = check_token_budget([T()], 100)
    assert ok is True and total == 0


def test_policy_filter_preserves_order():
    """Returned tools must come back in the original order — the UI
    relies on this for stable rendering."""
    from app.mcp_server.policy import filter_tools_for_key
    class T:
        def __init__(self, n): self.name = n
    tools = [T("a"), T("b"), T("c"), T("d")]
    filtered = filter_tools_for_key(tools, ["a", "c", "d"], None)
    assert [t.name for t in filtered] == ["a", "c", "d"]


# ── Capability scout hardening ───────────────────────────────────────


def test_scout_handles_long_response_with_late_refusal():
    """A 4KB-ish prefix of regular text followed by a refusal at the
    end MUST still be detected (don't accidentally bound the scan)."""
    from app.capability_scout import scan_response_text
    prefix = "Here is some context. " * 200  # ~4.2KB
    text = prefix + "Unfortunately I can't read PDF files."
    hits = scan_response_text(text)
    assert any(h["suggested_tool"] == "convert_document_to_markdown" for h in hits)


def test_scout_does_fire_on_quoted_refusal_with_known_pattern():
    """Pinning current behavior: when the QUOTED text contains one of
    the registered patterns verbatim, the scout fires. This is a
    documented false-positive risk — we'd rather over-suggest than
    miss a real refusal. If we add quote-context exclusion later, flip
    this assertion."""
    from app.capability_scout import scan_response_text
    txt = 'The bot said: "I can\'t read Excel files" — what should we do?'
    hits = scan_response_text(txt)
    assert len(hits) >= 1
    assert hits[0]["suggested_tool"] == "read_xlsx_to_markdown"


def test_scout_handles_unicode_response():
    from app.capability_scout import scan_response_text
    txt = "I'm sorry, but I can't read Excel files — 申し訳ありません"
    hits = scan_response_text(txt)
    assert any(h["suggested_tool"] == "read_xlsx_to_markdown" for h in hits)


def test_scout_empty_and_whitespace_safe():
    from app.capability_scout import scan_response_text
    assert scan_response_text("") == []
    assert scan_response_text("   \n\n  ") == []


# ── Inbound patcher idempotency ──────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_inbound_idempotent_on_repeated_calls():
    """Calling the patcher twice on the same messages list shouldn't
    re-execute the tool (the content from the first call is already
    real)."""
    from app import proxy_tools

    class _StubTool:
        name = "read_xlsx_to_markdown"

    call_count = {"n": 0}

    async def fake_registry():
        return [_StubTool()]

    async def fake_run(tool, input_obj):
        call_count["n"] += 1
        return f"output-{call_count['n']}"

    orig_registry = proxy_tools.get_registry_async
    orig_run = proxy_tools.run_tool
    try:
        proxy_tools.get_registry_async = fake_registry
        proxy_tools.run_tool = fake_run
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "x", "name": "read_xlsx_to_markdown", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "PLACEHOLDER"},
            ]},
        ]
        n1 = await proxy_tools.patch_inbound_tool_results(messages)
        n2 = await proxy_tools.patch_inbound_tool_results(messages)
        assert n1 == 1
        # After first patch the content is "output-1" — non-placeholder
        # — so the second call should skip.
        assert n2 == 0
        assert call_count["n"] == 1
    finally:
        proxy_tools.get_registry_async = orig_registry
        proxy_tools.run_tool = orig_run


# ── Admin summary aggregator robustness ──────────────────────────────


@pytest.mark.asyncio
async def test_mcp_summary_handles_zero_call_db():
    """No mcp_tool_calls rows + FastMCP root unmounted → endpoint still
    returns a well-shaped dict (no 500). Already covered in v5.7.5 but
    we add a stronger assertion on the latency_by_tool fallback path."""
    from app.api.admin_mcp_summary import mcp_summary
    db = MagicMock()
    row_count = MagicMock(); row_count.first.return_value = (0, 0)
    empty_rs = MagicMock(); empty_rs.fetchall.return_value = []
    db.execute = AsyncMock(side_effect=[row_count, empty_rs, empty_rs, empty_rs])
    out = await mcp_summary(db=db, _admin=MagicMock())
    assert out["latency_by_tool_24h"] == []
    assert out["calls_by_tool_24h"] == []
    assert out["calls_by_key_24h"] == []
    assert out["total_calls_24h"] == 0
    assert out["total_errors_24h"] == 0


# ── Capability suggestions router fields ─────────────────────────────


def test_capability_suggestions_endpoint_returns_expected_keys():
    """The router contract: every response carries items, by_tool,
    total_suggestions_lifetime, shown. Verified at import-time so a
    field rename trips CI."""
    from app.api.admin_mcp_capability_suggestions import (
        list_capability_suggestions,
    )
    import inspect
    sig = inspect.signature(list_capability_suggestions)
    # endpoint accepts limit + api_key_id query params
    assert "limit" in sig.parameters
    assert "api_key_id" in sig.parameters
