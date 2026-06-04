"""v5.0.3 — POST /v1/responses translation shim.

Pins the field-by-field translation between OpenAI Responses-shape and
ChatCompletions-shape. The full request lifecycle (auth + compliance +
dispatch) is tested via the chat_completions test suite; here we only
nail down the boundary.
"""
import pytest

from app.api.responses import _translate_request, _translate_response


def test_translate_request_string_input():
    out = _translate_request({"model": "gpt-4o", "input": "hello"})
    assert out["model"] == "gpt-4o"
    assert out["messages"] == [{"role": "user", "content": "hello"}]


def test_translate_request_instructions_become_system():
    out = _translate_request({
        "model": "gpt-4o",
        "instructions": "Be concise.",
        "input": "hello",
    })
    assert out["messages"] == [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "hello"},
    ]


def test_translate_request_message_items_flatten_text_content():
    out = _translate_request({
        "model": "gpt-4o",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "What's"},
                    {"type": "input_text", "text": "the weather?"},
                ],
            }
        ],
    })
    assert out["messages"] == [{"role": "user", "content": "What's\nthe weather?"}]


def test_translate_request_max_output_tokens_renamed():
    out = _translate_request({"model": "gpt-4o", "input": "x", "max_output_tokens": 256})
    assert out["max_tokens"] == 256
    assert "max_output_tokens" not in out


def test_translate_request_reasoning_effort():
    out = _translate_request({
        "model": "o3",
        "input": "x",
        "reasoning": {"effort": "high"},
    })
    assert out["reasoning_effort"] == "high"


def test_translate_request_drops_unsupported_state_fields():
    out = _translate_request({
        "model": "gpt-4o",
        "input": "x",
        "previous_response_id": "resp_abc",
        "store": True,
        "metadata": {"foo": "bar"},
        "include": ["reasoning"],
    })
    assert "previous_response_id" not in out
    assert "store" not in out
    assert "metadata" not in out
    assert "include" not in out


def test_translate_request_passes_through_common_fields():
    out = _translate_request({
        "model": "gpt-4o",
        "input": "x",
        "temperature": 0.3,
        "top_p": 0.9,
        "seed": 42,
        "stream": False,
        "user": "u-1",
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "response_format": {"type": "json_object"},
    })
    assert out["temperature"] == 0.3
    assert out["top_p"] == 0.9
    assert out["seed"] == 42
    assert out["stream"] is False
    assert out["user"] == "u-1"
    assert out["tools"] == [{"type": "function", "function": {"name": "f"}}]
    assert out["tool_choice"] == "auto"
    assert out["parallel_tool_calls"] is True
    assert out["response_format"] == {"type": "json_object"}


def test_translate_request_image_block():
    out = _translate_request({
        "model": "gpt-4o",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "describe this"},
                    {"type": "input_image", "image_url": "https://example/cat.png"},
                ],
            }
        ],
    })
    content = out["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "describe this"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "https://example/cat.png"}}


def test_translate_request_function_call_replay():
    out = _translate_request({
        "model": "gpt-4o",
        "input": [
            {"type": "function_call", "call_id": "c1", "name": "search",
             "arguments": '{"q":"x"}'},
            {"type": "function_call_output", "call_id": "c1", "output": "result"},
        ],
    })
    assert out["messages"][0]["role"] == "assistant"
    assert out["messages"][0]["tool_calls"][0]["function"]["name"] == "search"
    assert out["messages"][1]["role"] == "tool"
    assert out["messages"][1]["tool_call_id"] == "c1"
    assert out["messages"][1]["content"] == "result"


def test_translate_response_basic():
    cc = {
        "id": "chatcmpl-abc",
        "model": "gpt-4o-2024-08-06",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Hello!"},
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    r = _translate_response(cc)
    assert r["object"] == "response"
    assert r["status"] == "completed"
    assert r["model"] == "gpt-4o-2024-08-06"
    assert r["id"].startswith("resp_")
    assert r["output"][0]["type"] == "message"
    assert r["output"][0]["content"][0] == {
        "type": "output_text", "text": "Hello!", "annotations": [],
    }
    assert r["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_translate_response_length_finish_is_incomplete():
    cc = {
        "model": "gpt-4o",
        "choices": [{"finish_reason": "length",
                     "message": {"role": "assistant", "content": "partial"}}],
        "usage": {},
    }
    r = _translate_response(cc)
    assert r["status"] == "incomplete"


def test_translate_response_tool_calls_become_function_call_items():
    cc = {
        "model": "gpt-4o",
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"y"}'},
                }],
            },
        }],
        "usage": {},
    }
    r = _translate_response(cc)
    fc = [i for i in r["output"] if i["type"] == "function_call"]
    assert len(fc) == 1
    assert fc[0]["name"] == "lookup"
    assert fc[0]["arguments"] == '{"q":"y"}'
    assert fc[0]["call_id"] == "call_x"


def test_translate_response_carries_reasoning_tokens():
    cc = {
        "model": "o3",
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 100,
            "total_tokens": 105,
            "completion_tokens_details": {"reasoning_tokens": 80},
        },
    }
    r = _translate_response(cc)
    assert r["usage"]["output_tokens_details"]["reasoning_tokens"] == 80


def test_translate_response_empty_content_omits_message_item():
    """A pure tool_calls reply (content=None) should not emit a message
    output item with empty text — only the function_call item(s)."""
    cc = {
        "model": "gpt-4o",
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c", "type": "function",
                                "function": {"name": "f", "arguments": "{}"}}],
            },
        }],
        "usage": {},
    }
    r = _translate_response(cc)
    types = [i["type"] for i in r["output"]]
    assert "message" not in types
    assert "function_call" in types
