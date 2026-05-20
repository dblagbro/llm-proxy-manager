"""v4.3.8 — BUG-047: Anthropic→OpenAI/Cohere tool-def translation gate.

Closes BUG-047 surfaced during the 2026-05-20 proactive-monitoring
sweep: requests carrying Anthropic-shape tool DEFINITIONS at the body
level (``{"tools":[{"name":"x","input_schema":{...}}]}``) but no
tool_use/tool_result message blocks were falling through the
cross-family translation gate in ``app/api/messages.py`` and
400'ing on OpenAI/Cohere upstreams with
``missing required field: 'type'``.

The fix adds a ``has_anthropic_tool_defs(tools)`` helper in
``app/routing/tool_content.py`` and extends the
``_needs_openai_translation`` condition to include this case.

These tests pin the helper's classification matrix + a regression
guard against re-narrowing the gate.
"""
from __future__ import annotations

import pytest

from app.routing.tool_content import (
    has_anthropic_tool_content,
    has_anthropic_tool_defs,
)


# ── has_anthropic_tool_defs ───────────────────────────────────────


def test_returns_false_for_none():
    assert has_anthropic_tool_defs(None) is False


def test_returns_false_for_empty_list():
    assert has_anthropic_tool_defs([]) is False


def test_anthropic_shape_with_input_schema():
    tools = [{
        "name": "get_weather",
        "description": "Look up the weather",
        "input_schema": {"type": "object",
                          "properties": {"city": {"type": "string"}}},
    }]
    assert has_anthropic_tool_defs(tools) is True


def test_openai_shape_returns_false():
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Look up the weather",
            "parameters": {"type": "object",
                            "properties": {"city": {"type": "string"}}},
        },
    }]
    assert has_anthropic_tool_defs(tools) is False


def test_partial_openai_shape_treated_as_anthropic():
    """A tool that has type=function but lacks the nested `function`
    key is malformed OpenAI shape — treat as Anthropic so the
    translator gets a chance to fix it."""
    tools = [{"type": "function", "name": "x"}]
    assert has_anthropic_tool_defs(tools) is True


def test_missing_type_treated_as_anthropic():
    """A tool with no `type` field can't be OpenAI shape."""
    tools = [{"name": "x", "description": "y"}]
    assert has_anthropic_tool_defs(tools) is True


def test_wrong_type_treated_as_anthropic():
    tools = [{"type": "tool", "function": {"name": "x"}}]
    assert has_anthropic_tool_defs(tools) is True


def test_mixed_anthropic_and_openai_returns_true():
    """If ANY tool is Anthropic-shape, the gate must fire — translator
    re-emits the whole list in OpenAI shape so leaving Anthropic items
    untranslated would still 400."""
    tools = [
        {"type": "function", "function": {"name": "openai_shape"}},
        {"name": "anthropic_shape", "input_schema": {}},
    ]
    assert has_anthropic_tool_defs(tools) is True


def test_non_dict_items_ignored():
    """Defensive: skip non-dict items, don't crash."""
    tools = [None, "not-a-dict", 42,
             {"type": "function", "function": {"name": "ok"}}]
    assert has_anthropic_tool_defs(tools) is False


def test_only_input_schema_field_present():
    """Empty Anthropic-shape tool — still detected by input_schema."""
    tools = [{"input_schema": {}}]
    assert has_anthropic_tool_defs(tools) is True


# ── has_anthropic_tool_content (regression) ───────────────────────


def test_has_anthropic_tool_content_unaffected():
    """The original helper for message-block detection must remain
    untouched by the v4.3.8 changes."""
    msgs = [{"role": "user", "content": [
        {"type": "tool_use", "id": "tu_x", "name": "get_weather", "input": {}}
    ]}]
    assert has_anthropic_tool_content(msgs) is True
    assert has_anthropic_tool_content([]) is False
    # text-only messages still don't trigger
    assert has_anthropic_tool_content(
        [{"role": "user", "content": "hi"}]
    ) is False


# ── Independence: tool-defs vs tool-blocks are orthogonal ────────


def test_tool_defs_and_blocks_are_independent_signals():
    """The two helpers detect different things — confirm a request can
    have tool defs without blocks (first-turn case from BUG-047) and
    vice versa (a follow-up turn after Anthropic responded with
    tool_use blocks)."""
    # First turn: defs but no blocks
    defs_only_body_tools = [{"name": "x", "input_schema": {}}]
    defs_only_messages = [{"role": "user", "content": "use the tool"}]
    assert has_anthropic_tool_defs(defs_only_body_tools) is True
    assert has_anthropic_tool_content(defs_only_messages) is False

    # Follow-up turn: blocks but no defs (rare; usually both present)
    blocks_only_messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_x", "content": "42"}
    ]}]
    assert has_anthropic_tool_content(blocks_only_messages) is True
    assert has_anthropic_tool_defs(None) is False
