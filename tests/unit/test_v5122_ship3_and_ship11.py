"""v5.12.2 — Ship 3 (accept handler) + Ship 1.1 (MCP-native dual-emit
notification buffer) from the v5.10 design doc.

Ship 3 = caller can opt-in to a suggested tool via X-Proxy-Accept-MCP
header. Proxy validates against the MCP catalog, mutates the api_key's
mcp_tools_allow list, writes a compliance_policy_changes audit row.

Ship 1.1 = per-api_key notification buffer for callers with active /mcp
sessions. Suggestion-emit pushes a notification body when the header
fires; FastMCP tool-call hook drains it on the next tool call. Pattern
borrowed from ccproxy's NotificationBuffer (TTL + overflow markers).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path


# ── Ship 3 — accept-header parsing ─────────────────────────────────────


def test_accept_header_module_exists():
    from app.capability_scout.accept_handler import (
        HEADER_NAME, RESPONSE_STATUS_HEADER, WARNING_HEADER,
        process_accept_header,
    )
    assert HEADER_NAME == "X-Proxy-Accept-MCP"
    assert RESPONSE_STATUS_HEADER == "X-Proxy-MCP-Accept-Status"
    assert WARNING_HEADER == "Warning"


def test_accept_header_parses_comma_separated():
    from app.capability_scout.accept_handler import _parse_accept_header
    assert _parse_accept_header("read_xlsx_to_markdown") == ["read_xlsx_to_markdown"]
    assert _parse_accept_header(" tool_a , tool_b , tool_c ") == ["tool_a", "tool_b", "tool_c"]
    # Tolerate the "tool=name" prefix form
    assert _parse_accept_header("tool=read_xlsx_to_markdown") == ["read_xlsx_to_markdown"]
    # Dedupe + preserve order
    assert _parse_accept_header("a,b,a,c") == ["a", "b", "c"]
    # Empty / whitespace
    assert _parse_accept_header("") == []
    assert _parse_accept_header("  ") == []
    assert _parse_accept_header(",,,") == []


def test_accept_handler_wired_into_messages():
    src = Path("app/api/messages.py").read_text()
    assert "from app.capability_scout.accept_handler import process_accept_header" in src
    assert 'request.headers.get("X-Proxy-Accept-MCP")' in src


def test_accept_handler_wired_into_completions():
    src = Path("app/api/completions.py").read_text()
    assert "from app.capability_scout.accept_handler import process_accept_header" in src


def test_accept_audit_scope_per_key():
    """Adoption audit row MUST use scope='per_key' + target_id=api_key_id
    per operator decision 2026-06-30. Matches v5.1.2 retention-edit
    pattern."""
    src = Path("app/capability_scout/accept_handler.py").read_text()
    assert 'scope="per_key"' in src
    assert 'target_id=api_key_id' in src
    assert 'reason="mcp_tool_adopted_via_accept_header"' in src


# ── Ship 1.1 — notification buffer ────────────────────────────────────


def test_buffer_module_constants():
    from app.capability_scout.suggestion_buffer_mcp import (
        _MAX_PER_KEY, _TTL_SEC, BUFFER,
        push_suggestion_notification, drain_pending,
    )
    assert _MAX_PER_KEY == 32
    assert _TTL_SEC == 3600.0


def test_buffer_push_drain_round_trip():
    from app.capability_scout.suggestion_buffer_mcp import BUFFER
    # Use a synthetic key to avoid colliding with anything live.
    key = f"test-key-{time.time()}"
    BUFFER.push(key, {"type": "proxy_mcp_suggestion", "tool": "x"})
    BUFFER.push(key, {"type": "proxy_mcp_suggestion", "tool": "y"})
    drained = BUFFER.drain(key)
    assert len(drained) == 2
    assert drained[0]["tool"] == "x"
    assert drained[1]["tool"] == "y"
    # Drain is consuming — second call returns empty.
    assert BUFFER.drain(key) == []


def test_buffer_overflow_marker_uses_ccproxy_compat_event_type():
    """If we accumulate past the per-key cap, the drain MUST surface a
    synthetic overflow marker with event type ``ccproxy_buffer_overflow``
    so consumer tools that already parse ccproxy's overflow markers
    accept ours without changes."""
    from app.capability_scout.suggestion_buffer_mcp import BUFFER, _MAX_PER_KEY
    key = f"test-overflow-{time.time()}"
    # Push more than the cap.
    for i in range(_MAX_PER_KEY + 3):
        BUFFER.push(key, {"type": "proxy_mcp_suggestion", "tool": f"x{i}"})
    drained = BUFFER.drain(key)
    # First element should be the overflow marker.
    assert drained[0]["type"] == "ccproxy_buffer_overflow"
    assert drained[0]["dropped_events"] == 3
    # Remainder = cap.
    assert len(drained) - 1 == _MAX_PER_KEY


def test_buffer_drain_wired_into_mcp_tool_call():
    src = Path("app/mcp_server/server.py").read_text()
    assert "from app.capability_scout.suggestion_buffer_mcp import drain_pending" in src
    assert "drain_pending(api_key_id)" in src


def test_suggestion_emit_pushes_to_buffer():
    src = Path("app/capability_scout/suggestion_emit.py").read_text()
    assert "from app.capability_scout.suggestion_buffer_mcp import push_suggestion_notification" in src
    assert "push_suggestion_notification(api_key_id, tool, score" in src


# ── version ──────────────────────────────────────────────────────────


def test_version_bumped():
    src = Path("app/__version__.py").read_text()
    assert '"5.12.2"' in src
