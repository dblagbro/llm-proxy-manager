"""Tests for the v3.10.12 bug-log fixes.

- BUG-028(a): an Anthropic assistant turn with neither text nor tool_use
  must not translate to {role:assistant, content:null, no tool_calls}.
- BUG-028(b): a tool_result is emitted as an OpenAI role:"tool" message
  only when it answers the IMMEDIATELY preceding assistant turn; an
  orphaned OR misordered tool_result degrades to plain user text.
- BUG-037: the streaming claude-oauth read timeout is tighter than the
  non-streaming one (bounds a hung stream).
"""
from __future__ import annotations

from app.api._oauth_chat_translate import anthropic_messages_to_openai


def test_bug028a_empty_assistant_block_gets_placeholder():
    """An assistant turn carrying only a `thinking` block (dropped in
    cross-family translation) must not emit content:null without
    tool_calls — OpenAI rejects that."""
    out = anthropic_messages_to_openai([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}]},
    ])
    asst = [m for m in out if m["role"] == "assistant"]
    assert len(asst) == 1
    assert asst[0]["content"] is not None, "content:null with no tool_calls is invalid"
    assert asst[0]["content"], "placeholder content must be non-empty"
    assert "tool_calls" not in asst[0]


def test_bug028a_assistant_with_tool_use_keeps_null_content():
    """Regression guard — an assistant turn with tool_use (and no text)
    still uses content:null, which OpenAI allows when tool_calls present."""
    out = anthropic_messages_to_openai([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}}]},
    ])
    asst = [m for m in out if m["role"] == "assistant"][0]
    assert asst["content"] is None
    assert asst["tool_calls"][0]["id"] == "t1"


def test_bug028b_wellformed_tool_result_becomes_role_tool():
    """A tool_result that answers the immediately preceding assistant
    turn translates to a proper OpenAI role:"tool" message."""
    out = anthropic_messages_to_openai([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "f", "input": {"x": 1}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ])
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "t1"


def test_bug028b_misordered_tool_result_degrades_to_user_text():
    """A tool_result whose tool_use was declared by an EARLIER assistant
    turn (not the immediately preceding one) must NOT become a dangling
    role:"tool" message — OpenAI 400s on that. It degrades to user text."""
    out = anthropic_messages_to_openai([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "more"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "late result"}]},
    ])
    assert not any(m["role"] == "tool" for m in out), (
        "a misordered tool_result must not be emitted as role:tool"
    )
    assert any(m["role"] == "user" and "tool result" in str(m.get("content", "")).lower()
               for m in out), "misordered tool_result should appear as user text"


def test_bug028b_orphan_tool_result_still_degrades():
    """Regression guard — a conversation window beginning mid-tool-
    exchange (tool_result with no preceding assistant) still degrades to
    user text (the adjacency rule subsumes the old global pre-scan)."""
    out = anthropic_messages_to_openai([
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tX", "content": "r"}]},
    ])
    assert not any(m["role"] == "tool" for m in out)


def test_bug037_streaming_timeout_is_tighter():
    from app.api._messages_streaming import (
        _CLAUDE_OAUTH_TIMEOUT, _CLAUDE_OAUTH_STREAM_TIMEOUT,
    )
    assert _CLAUDE_OAUTH_STREAM_TIMEOUT.read < _CLAUDE_OAUTH_TIMEOUT.read
    # connect timeout stays small on both (the 2026-05-05 outage fix)
    assert _CLAUDE_OAUTH_STREAM_TIMEOUT.connect == _CLAUDE_OAUTH_TIMEOUT.connect
