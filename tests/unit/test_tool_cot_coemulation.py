"""Tool + CoT co-emulation (v4.1.1).

When a request needs BOTH tool-calling and reasoning and the chosen provider
is native in neither, the tool prompt is reasoning-prefixed: the model thinks
step by step inside a <thinking> block, then emits <tool_call> blocks.
"""
from __future__ import annotations

from app.cot.tool_emulation import (
    build_anthropic_tool_prompt, build_openai_tool_prompt,
    parse_tool_calls, strip_thinking,
)

_ANTHROPIC_TOOLS = [{
    "name": "get_weather", "description": "Get the weather",
    "input_schema": {"type": "object",
                     "properties": {"city": {"type": "string"}},
                     "required": ["city"]},
}]
_OPENAI_TOOLS = [{
    "type": "function",
    "function": {"name": "get_weather", "description": "Get the weather",
                 "parameters": {"type": "object",
                                "properties": {"city": {"type": "string"}}}},
}]


# ── reasoning-prefixed tool prompt ───────────────────────────────────────────

def test_anthropic_prompt_reasoning_prefix_toggles():
    plain = build_anthropic_tool_prompt(_ANTHROPIC_TOOLS, with_reasoning=False)
    coemul = build_anthropic_tool_prompt(_ANTHROPIC_TOOLS, with_reasoning=True)
    assert "<thinking>" not in plain
    assert "<thinking>" in coemul                       # reasoning preamble added
    assert "get_weather" in plain and "get_weather" in coemul  # tool schema intact
    assert "<tool_call>" in coemul                      # tool-call format intact


def test_openai_prompt_reasoning_prefix_toggles():
    plain = build_openai_tool_prompt(_OPENAI_TOOLS, with_reasoning=False)
    coemul = build_openai_tool_prompt(_OPENAI_TOOLS, with_reasoning=True)
    assert "<thinking>" not in plain
    assert "<thinking>" in coemul and "get_weather" in coemul


def test_reasoning_prompt_default_is_off():
    # default must stay False — plain tool requests are unchanged
    assert "<thinking>" not in build_anthropic_tool_prompt(_ANTHROPIC_TOOLS)
    assert "<thinking>" not in build_openai_tool_prompt(_OPENAI_TOOLS)


# ── strip_thinking ───────────────────────────────────────────────────────────

def test_strip_thinking_removes_block():
    txt = "<thinking>let me reason this through</thinking>\nThe answer is 42."
    assert strip_thinking(txt) == "The answer is 42."


def test_strip_thinking_noop_when_absent():
    assert strip_thinking("just a plain answer") == "just a plain answer"


def test_strip_thinking_handles_empty():
    assert strip_thinking("") == ""
    assert strip_thinking(None) == ""


# ── the co-emulation response shape: reasoning THEN tool calls ───────────────

def test_parse_tool_calls_ignores_thinking_preamble():
    txt = ('<thinking>The user wants the weather in Paris; I should call '
           'get_weather.</thinking>\n'
           '<tool_call>\n{"name": "get_weather", "input": {"city": "Paris"}}\n'
           '</tool_call>')
    calls = parse_tool_calls(txt)
    assert len(calls) == 1
    assert calls[0]["name"] == "get_weather"
    assert calls[0]["input"] == {"city": "Paris"}


def test_parse_tool_calls_parallel_after_thinking():
    txt = ('<thinking>Two cities needed.</thinking>\n'
           '<tool_call>\n{"name": "get_weather", "input": {"city": "Paris"}}\n</tool_call>\n'
           '<tool_call>\n{"name": "get_weather", "input": {"city": "Tokyo"}}\n</tool_call>')
    calls = parse_tool_calls(txt)
    assert [c["input"]["city"] for c in calls] == ["Paris", "Tokyo"]
