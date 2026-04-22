"""Unit tests for tool emulation — multi-tag parsing, normalisation, prompt building."""
import json
import pytest
from app.cot.tool_emulation import (
    parse_tool_call,
    build_anthropic_tool_prompt,
    build_openai_tool_prompt,
    normalize_anthropic_messages,
    normalize_openai_messages,
)

TOOL_DEF_ANTHROPIC = {
    "name": "read_file",
    "description": "Read a file from disk",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File path"}},
        "required": ["path"],
    },
}

TOOL_DEF_OPENAI = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from disk",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


# ── parse_tool_call — tag variants ────────────────────────────────────────────

def test_parse_tool_call_standard_tag():
    text = '<tool_call>\n{"name": "read_file", "input": {"path": "/tmp/test"}}\n</tool_call>'
    result = parse_tool_call(text)
    assert result is not None
    assert result["name"] == "read_file"
    assert result["input"] == {"path": "/tmp/test"}


def test_parse_tool_code_tag():
    text = '<tool_code>\n{"name": "read_file", "input": {"path": "/etc/hosts"}}\n</tool_code>'
    result = parse_tool_call(text)
    assert result is not None
    assert result["name"] == "read_file"


def test_parse_function_call_tag():
    text = '<function_call>\n{"name": "search", "input": {"query": "hello"}}\n</function_call>'
    result = parse_tool_call(text)
    assert result is not None
    assert result["name"] == "search"
    assert result["input"]["query"] == "hello"


def test_parse_tool_use_tag():
    text = '<tool_use>\n{"name": "list_dir", "input": {"path": "/"}}\n</tool_use>'
    result = parse_tool_call(text)
    assert result is not None
    assert result["name"] == "list_dir"


def test_parse_returns_none_for_plain_text():
    assert parse_tool_call("I cannot read files directly.") is None


def test_parse_returns_none_for_malformed_json():
    assert parse_tool_call("<tool_call>not json</tool_call>") is None


# ── parse_tool_call — field name normalisation ────────────────────────────────

def test_parse_normalises_function_name_to_name():
    text = '<tool_call>\n{"function_name": "read_file", "input": {"path": "/tmp"}}\n</tool_call>'
    result = parse_tool_call(text)
    assert result is not None
    assert result["name"] == "read_file"
    assert "function_name" not in result


def test_parse_normalises_tool_name_to_name():
    text = '<tool_call>\n{"tool_name": "write_file", "input": {"path": "/tmp", "content": "hi"}}\n</tool_call>'
    result = parse_tool_call(text)
    assert result is not None
    assert result["name"] == "write_file"
    assert "tool_name" not in result


def test_parse_normalises_parameters_to_input():
    text = '<tool_call>\n{"name": "read_file", "parameters": {"path": "/tmp"}}\n</tool_call>'
    result = parse_tool_call(text)
    assert result is not None
    assert result["input"] == {"path": "/tmp"}
    assert "parameters" not in result


def test_parse_normalises_arguments_to_input():
    text = '<tool_call>\n{"name": "read_file", "arguments": {"path": "/tmp"}}\n</tool_call>'
    result = parse_tool_call(text)
    assert result is not None
    assert result["input"] == {"path": "/tmp"}
    assert "arguments" not in result


def test_parse_normalises_args_to_input():
    text = '<tool_call>\n{"name": "read_file", "args": {"path": "/tmp"}}\n</tool_call>'
    result = parse_tool_call(text)
    assert result is not None
    assert result["input"] == {"path": "/tmp"}


def test_parse_defaults_input_to_empty_dict():
    text = '<tool_call>\n{"name": "get_time"}\n</tool_call>'
    result = parse_tool_call(text)
    assert result is not None
    assert result["input"] == {}


def test_parse_returns_none_when_no_name():
    text = '<tool_call>\n{"parameters": {"path": "/tmp"}}\n</tool_call>'
    assert parse_tool_call(text) is None


# ── parse_tool_call — surrounding text ───────────────────────────────────────

def test_parse_ignores_surrounding_text():
    text = 'I will call the tool now:\n<tool_call>\n{"name": "read_file", "input": {"path": "/x"}}\n</tool_call>\nDone.'
    result = parse_tool_call(text)
    assert result is not None
    assert result["name"] == "read_file"


# ── build_anthropic_tool_prompt ───────────────────────────────────────────────

def test_build_anthropic_prompt_contains_tool_name():
    prompt = build_anthropic_tool_prompt([TOOL_DEF_ANTHROPIC])
    assert "read_file" in prompt


def test_build_anthropic_prompt_contains_description():
    prompt = build_anthropic_tool_prompt([TOOL_DEF_ANTHROPIC])
    assert "Read a file from disk" in prompt


def test_build_anthropic_prompt_contains_parameter_name():
    prompt = build_anthropic_tool_prompt([TOOL_DEF_ANTHROPIC])
    assert "path" in prompt


def test_build_anthropic_prompt_contains_tool_call_instruction():
    prompt = build_anthropic_tool_prompt([TOOL_DEF_ANTHROPIC])
    assert "<tool_call>" in prompt


def test_build_anthropic_prompt_multiple_tools():
    tools = [
        TOOL_DEF_ANTHROPIC,
        {"name": "write_file", "description": "Write data", "input_schema": {"type": "object", "properties": {}}},
    ]
    prompt = build_anthropic_tool_prompt(tools)
    assert "read_file" in prompt
    assert "write_file" in prompt


# ── build_openai_tool_prompt ──────────────────────────────────────────────────

def test_build_openai_prompt_contains_tool_name():
    prompt = build_openai_tool_prompt([TOOL_DEF_OPENAI])
    assert "read_file" in prompt


def test_build_openai_prompt_contains_description():
    prompt = build_openai_tool_prompt([TOOL_DEF_OPENAI])
    assert "Read a file from disk" in prompt


def test_build_openai_prompt_contains_parameter():
    prompt = build_openai_tool_prompt([TOOL_DEF_OPENAI])
    assert "path" in prompt


# ── normalize_anthropic_messages ──────────────────────────────────────────────

def test_normalize_anthropic_string_content_unchanged():
    msgs = [{"role": "user", "content": "hello"}]
    result = normalize_anthropic_messages(msgs)
    assert result == [{"role": "user", "content": "hello"}]


def test_normalize_anthropic_text_block():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hello world"}]}]
    result = normalize_anthropic_messages(msgs)
    assert result[0]["content"] == "hello world"


def test_normalize_anthropic_tool_use_block():
    msgs = [{"role": "assistant", "content": [
        {"type": "tool_use", "name": "read_file", "input": {"path": "/tmp"}},
    ]}]
    result = normalize_anthropic_messages(msgs)
    content = result[0]["content"]
    assert "<tool_call>" in content
    assert "read_file" in content
    assert "/tmp" in content


def test_normalize_anthropic_tool_result_block():
    msgs = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tid123", "content": "file contents here"},
    ]}]
    result = normalize_anthropic_messages(msgs)
    content = result[0]["content"]
    assert "tool_result" in content
    assert "tid123" in content
    assert "file contents here" in content


def test_normalize_anthropic_tool_result_list_content():
    msgs = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tid",
         "content": [{"type": "text", "text": "result data"}]},
    ]}]
    result = normalize_anthropic_messages(msgs)
    assert "result data" in result[0]["content"]


def test_normalize_anthropic_skips_empty_messages():
    msgs = [{"role": "user", "content": []}]
    result = normalize_anthropic_messages(msgs)
    assert result == []


# ── normalize_openai_messages ─────────────────────────────────────────────────

def test_normalize_openai_regular_message():
    msgs = [{"role": "user", "content": "hello"}]
    result = normalize_openai_messages(msgs)
    assert result == [{"role": "user", "content": "hello"}]


def test_normalize_openai_tool_role_becomes_user():
    msgs = [{"role": "tool", "tool_call_id": "call_abc", "content": "result data"}]
    result = normalize_openai_messages(msgs)
    assert result[0]["role"] == "user"
    assert "call_abc" in result[0]["content"]
    assert "result data" in result[0]["content"]


def test_normalize_openai_tool_calls_in_assistant():
    msgs = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "read_file", "arguments": json.dumps({"path": "/tmp"})}}
    ]}]
    result = normalize_openai_messages(msgs)
    assert result[0]["role"] == "assistant"
    assert "<tool_call>" in result[0]["content"]
    assert "read_file" in result[0]["content"]


def test_normalize_openai_tool_calls_invalid_json_args():
    msgs = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "read_file", "arguments": "not-json"}}
    ]}]
    result = normalize_openai_messages(msgs)
    # Should not crash; content is still set with the tool call
    assert result[0]["role"] == "assistant"
    assert "read_file" in result[0]["content"]


def test_normalize_openai_preserves_system_role():
    msgs = [{"role": "system", "content": "You are helpful."}]
    result = normalize_openai_messages(msgs)
    assert result == [{"role": "system", "content": "You are helpful."}]
