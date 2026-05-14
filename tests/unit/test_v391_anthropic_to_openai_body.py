"""v3.9.1 (#269 Fixes A + B) — Anthropic→OpenAI body translation +
cross-family safety net.

Covers ``app.api._oauth_chat_translate.anthropic_to_openai_body``,
``anthropic_messages_to_openai``, ``anthropic_tools_to_openai``, and
``app.routing.tool_content.has_anthropic_tool_content``.

Replays the exact failing-traffic shape from activity_log id=169903
(bare ``[tool_result] PackageKit...``) to prove the translator produces
a valid OpenAI body.
"""
from __future__ import annotations

import json


# ── A: has_anthropic_tool_content ─────────────────────────────────


def test_has_tool_content_plain_text_false():
    from app.routing.tool_content import has_anthropic_tool_content
    msgs = [{"role": "user", "content": "hello"}]
    assert has_anthropic_tool_content(msgs) is False


def test_has_tool_content_detects_tool_result():
    from app.routing.tool_content import has_anthropic_tool_content
    msgs = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "x"},
        ]},
    ]
    assert has_anthropic_tool_content(msgs) is True


def test_has_tool_content_detects_tool_use_in_assistant():
    from app.routing.tool_content import has_anthropic_tool_content
    msgs = [
        {"role": "assistant", "content": [
            {"type": "text", "text": "I'll call a tool"},
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
        ]},
    ]
    assert has_anthropic_tool_content(msgs) is True


def test_has_tool_content_text_blocks_only_false():
    from app.routing.tool_content import has_anthropic_tool_content
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert has_anthropic_tool_content(msgs) is False


def test_has_tool_content_empty_messages_false():
    from app.routing.tool_content import has_anthropic_tool_content
    assert has_anthropic_tool_content([]) is False


# ── B: anthropic_messages_to_openai ───────────────────────────────


def test_pure_text_passthrough():
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    out = anthropic_messages_to_openai(
        [{"role": "user", "content": "hello"}],
    )
    assert out == [{"role": "user", "content": "hello"}]


def test_system_field_string_prepended():
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    out = anthropic_messages_to_openai(
        [{"role": "user", "content": "hi"}],
        body_system="be polite",
    )
    assert out[0] == {"role": "system", "content": "be polite"}
    assert out[1] == {"role": "user", "content": "hi"}


def test_system_field_list_prepended():
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    out = anthropic_messages_to_openai(
        [{"role": "user", "content": "hi"}],
        body_system=[{"type": "text", "text": "rule one"},
                     {"type": "text", "text": "rule two"}],
    )
    assert out[0]["role"] == "system"
    assert "rule one" in out[0]["content"]
    assert "rule two" in out[0]["content"]


def test_tool_use_block_in_assistant_becomes_tool_calls():
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    msgs = [
        {"role": "assistant", "content": [
            {"type": "text", "text": "calling tool"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
             "input": {"city": "Seattle"}},
        ]}
    ]
    out = anthropic_messages_to_openai(msgs)
    assert len(out) == 1
    m = out[0]
    assert m["role"] == "assistant"
    assert m["content"] == "calling tool"
    assert m["tool_calls"][0] == {
        "id": "toolu_1",
        "type": "function",
        "function": {
            "name": "get_weather",
            "arguments": json.dumps({"city": "Seattle"}),
        },
    }


def test_assistant_with_only_tool_use_has_null_content():
    """OpenAI: when an assistant has tool_calls and no text, content
    must be null (not empty string)."""
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    msgs = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
        ]}
    ]
    out = anthropic_messages_to_openai(msgs)
    assert out[0]["content"] is None
    assert out[0]["tool_calls"][0]["id"] == "t1"


def test_tool_result_block_in_user_becomes_role_tool():
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    msgs = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": "72F sunny"},
        ]}
    ]
    out = anthropic_messages_to_openai(msgs)
    assert len(out) == 1
    assert out[0] == {
        "role": "tool",
        "tool_call_id": "toolu_1",
        "content": "72F sunny",
    }


def test_empty_tool_result_content_gets_placeholder():
    """OpenAI rejects empty tool message content — translator MUST
    substitute a placeholder. This is the exact bug from
    activity_log id=169903 (empty `[tool_result]` block)."""
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    for empty in ("", None):
        msgs = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": empty},
            ]}
        ]
        out = anthropic_messages_to_openai(msgs)
        assert out[0]["content"] == "(no output)"


def test_tool_result_content_list_collapses_to_string():
    """Anthropic tool_result.content can be a list of blocks; OpenAI
    needs a single string."""
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    msgs = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "text", "text": "part 1"},
                {"type": "text", "text": "part 2"},
            ]},
        ]}
    ]
    out = anthropic_messages_to_openai(msgs)
    assert "part 1" in out[0]["content"]
    assert "part 2" in out[0]["content"]


def test_user_message_mixed_tool_result_and_text_splits():
    """Per OpenAI convention: tool_result becomes role:tool first,
    then any remaining user text becomes a separate role:user message."""
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    msgs = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "X"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "Y"},
            {"type": "text", "text": "now please continue"},
        ]}
    ]
    out = anthropic_messages_to_openai(msgs)
    assert len(out) == 3
    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "t1"
    assert out[1]["role"] == "tool"
    assert out[1]["tool_call_id"] == "t2"
    assert out[2] == {"role": "user", "content": "now please continue"}


def test_five_tool_results_in_one_user_message():
    """Spec test: 5+ blocks → 5+ role:tool messages in order."""
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    blocks = [
        {"type": "tool_result", "tool_use_id": f"t{i}", "content": f"r{i}"}
        for i in range(5)
    ]
    out = anthropic_messages_to_openai(
        [{"role": "user", "content": blocks}],
    )
    assert len(out) == 5
    for i, msg in enumerate(out):
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == f"t{i}"
        assert msg["content"] == f"r{i}"


def test_assistant_text_then_user_tool_result_round_trip():
    """Full assistant tool_calls → user tool_result conversation."""
    from app.api._oauth_chat_translate import anthropic_messages_to_openai
    msgs = [
        {"role": "user", "content": "what's the weather?"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "weather",
             "input": {"city": "SEA"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "72F"},
        ]},
    ]
    out = anthropic_messages_to_openai(msgs)
    assert out[0] == {"role": "user", "content": "what's the weather?"}
    assert out[1]["role"] == "assistant"
    assert out[1]["tool_calls"][0]["id"] == "t1"
    assert out[2] == {"role": "tool", "tool_call_id": "t1", "content": "72F"}


# ── anthropic_tools_to_openai ─────────────────────────────────────


def test_tools_translate_to_openai_shape():
    from app.api._oauth_chat_translate import anthropic_tools_to_openai
    out = anthropic_tools_to_openai([
        {"name": "get_weather", "description": "Get the weather",
         "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}},
    ])
    assert out == [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }]


def test_tools_none_returns_none():
    from app.api._oauth_chat_translate import anthropic_tools_to_openai
    assert anthropic_tools_to_openai(None) is None
    assert anthropic_tools_to_openai([]) is None


# ── anthropic_to_openai_body (full body) ──────────────────────────


def test_full_body_translation():
    from app.api._oauth_chat_translate import anthropic_to_openai_body
    body = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "system": "be polite",
        "messages": [
            {"role": "user", "content": "hi"},
        ],
        "tools": [
            {"name": "f", "description": "d",
             "input_schema": {"type": "object"}},
        ],
        "tool_choice": {"type": "auto"},
        "thinking": {"type": "enabled", "budget_tokens": 1024},  # should be dropped
    }
    out = anthropic_to_openai_body(body)
    assert out["model"] == "claude-3-5-sonnet"
    assert out["max_tokens"] == 1024
    assert "thinking" not in out  # dropped
    assert "system" not in out  # folded into messages
    assert out["messages"][0] == {"role": "system", "content": "be polite"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}
    assert out["tools"][0]["type"] == "function"
    assert out["tool_choice"] == "auto"


def test_tool_choice_any_becomes_required():
    from app.api._oauth_chat_translate import anthropic_to_openai_body
    body = {
        "model": "x", "max_tokens": 10,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "f", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "any"},
    }
    out = anthropic_to_openai_body(body)
    assert out["tool_choice"] == "required"


def test_tool_choice_specific_tool():
    from app.api._oauth_chat_translate import anthropic_to_openai_body
    body = {
        "model": "x", "max_tokens": 10,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "weather", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "tool", "name": "weather"},
    }
    out = anthropic_to_openai_body(body)
    assert out["tool_choice"] == {
        "type": "function",
        "function": {"name": "weather"},
    }


def test_stop_sequences_become_stop():
    from app.api._oauth_chat_translate import anthropic_to_openai_body
    body = {
        "model": "x", "max_tokens": 10,
        "messages": [{"role": "user", "content": "x"}],
        "stop_sequences": ["END", "STOP"],
    }
    out = anthropic_to_openai_body(body)
    assert out["stop"] == ["END", "STOP"]


# ── Replay of failing-traffic shape ───────────────────────────────


def test_failing_traffic_shape_from_id_169903_translates_cleanly():
    """activity_log id=169903 had a user message whose content was a
    bare ``[tool_result]`` block (Anthropic shape) — empty content
    after the [tool_result] marker. The translator MUST emit a valid
    OpenAI body where every role:tool message has non-empty content."""
    from app.api._oauth_chat_translate import anthropic_to_openai_body
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "system": "You are the coordinator daemon",
        "messages": [
            {"role": "user", "content": "Investigate this issue"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Checking logs..."},
                {"type": "tool_use", "id": "toolu_a", "name": "Bash",
                 "input": {"cmd": "ls /var/lib/coordinator-hub/"}},
            ]},
            # The empty-content tool_result that broke the upstream:
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_a", "content": ""},
            ]},
            # Bare tool_result (none/None-content variant):
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_b", "name": "Bash",
                 "input": {"cmd": "ls"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_b"},  # no content key
            ]},
        ],
    }
    out = anthropic_to_openai_body(body)

    # Every role:tool message has non-empty content
    tool_msgs = [m for m in out["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    for m in tool_msgs:
        assert m["content"]  # truthy
        assert m["content"] != ""

    # Assistant tool_calls present + matched to tool_use_ids
    asst_msgs = [m for m in out["messages"] if m["role"] == "assistant"]
    assert len(asst_msgs) == 2
    assert asst_msgs[0]["tool_calls"][0]["id"] == "toolu_a"
    assert asst_msgs[1]["tool_calls"][0]["id"] == "toolu_b"
    # tool_call_ids match
    assert tool_msgs[0]["tool_call_id"] == "toolu_a"
    assert tool_msgs[1]["tool_call_id"] == "toolu_b"


# ── Source-level wiring guards ─────────────────────────────────────


def test_messages_endpoint_wires_safety_net():
    from pathlib import Path
    src = Path("app/api/messages.py").read_text()
    assert "has_anthropic_tool_content" in src
    assert "_cross_family_skipped" in src
    assert "X-Cross-Family-Skipped" in src


def test_messages_endpoint_wires_translator():
    from pathlib import Path
    src = Path("app/api/messages.py").read_text()
    assert "anthropic_to_openai_body" in src
    assert "X-Cross-Family-Translated" in src


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 9, 1)
