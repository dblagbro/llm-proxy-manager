"""
Wire format serialization — Anthropic and OpenAI SSE chunks + JSON response builders.

All SSE event byte production lives here so format changes require one edit.
Used by cot/pipeline.py (thinking block primitives) and both endpoint handlers
(tool emulation response generators).
"""
from __future__ import annotations

import json
import secrets
from typing import AsyncIterator


# ── Anthropic SSE primitives (used by pipeline.py) ───────────────────────────

def sse_thinking_start(index: int) -> bytes:
    return f'data: {{"type":"content_block_start","index":{index},"content_block":{{"type":"thinking","thinking":""}}}}\n\n'.encode()


def sse_thinking_delta(index: int, text: str) -> bytes:
    escaped = json.dumps(text)[1:-1]
    return f'data: {{"type":"content_block_delta","index":{index},"delta":{{"type":"thinking_delta","thinking":"{escaped}"}}}}\n\n'.encode()


def sse_thinking_stop(index: int) -> bytes:
    return f'data: {{"type":"content_block_stop","index":{index}}}\n\n'.encode()


def sse_text_start(index: int) -> bytes:
    return f'data: {{"type":"content_block_start","index":{index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()


def sse_text_delta(index: int, text: str) -> bytes:
    escaped = json.dumps(text)[1:-1]
    return f'data: {{"type":"content_block_delta","index":{index},"delta":{{"type":"text_delta","text":"{escaped}"}}}}\n\n'.encode()


def sse_text_stop(index: int) -> bytes:
    return f'data: {{"type":"content_block_stop","index":{index}}}\n\n'.encode()


def sse_message_delta(stop_reason: str, input_tokens: int, output_tokens: int) -> bytes:
    return (
        f'data: {{"type":"message_delta","delta":{{"stop_reason":"{stop_reason}","stop_sequence":null}},'
        f'"usage":{{"input_tokens":{input_tokens},"output_tokens":{output_tokens}}}}}\n\n'
    ).encode()


def sse_done() -> bytes:
    return b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'


# ── Anthropic response generators (tool emulation) ───────────────────────────

async def anthropic_tool_sse(tool_name: str, tool_input: dict) -> AsyncIterator[bytes]:
    tool_id = f"toolu_{secrets.token_hex(8)}"
    input_json = json.dumps(tool_input)
    escaped = json.dumps(input_json)[1:-1]
    yield f'data: {{"type":"content_block_start","index":0,"content_block":{{"type":"tool_use","id":"{tool_id}","name":"{tool_name}","input":{{}}}}}}\n\n'.encode()
    yield f'data: {{"type":"content_block_delta","index":0,"delta":{{"type":"input_json_delta","partial_json":"{escaped}"}}}}\n\n'.encode()
    yield b'data: {"type":"content_block_stop","index":0}\n\n'
    yield b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":10}}\n\n'
    yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'


async def anthropic_text_sse(text: str) -> AsyncIterator[bytes]:
    yield b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    chunk = 80
    for i in range(0, len(text), chunk):
        piece = json.dumps(text[i:i + chunk])[1:-1]
        yield f'data: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":"{piece}"}}}}\n\n'.encode()
    yield b'data: {"type":"content_block_stop","index":0}\n\n'
    yield b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null}}\n\n'
    yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'


def anthropic_tool_response(tool_name: str, tool_input: dict, model: str) -> dict:
    return {
        "id": f"msg_emul_{secrets.token_hex(4)}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "tool_use", "id": f"toolu_{secrets.token_hex(8)}", "name": tool_name, "input": tool_input}],
        "model": model,
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def anthropic_text_response(text: str, model: str) -> dict:
    return {
        "id": f"msg_emul_{secrets.token_hex(4)}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


# ── OpenAI response generators (tool emulation) ──────────────────────────────

async def openai_tool_sse(tool_name: str, tool_input: dict) -> AsyncIterator[bytes]:
    call_id = f"call_{secrets.token_hex(8)}"
    msg_id = f"chatcmpl-emul-{secrets.token_hex(4)}"
    args_json = json.dumps(tool_input)
    escaped_args = json.dumps(args_json)[1:-1]
    yield (
        f'data: {{"id":"{msg_id}","object":"chat.completion.chunk","choices":[{{"index":0,'
        f'"delta":{{"role":"assistant","tool_calls":[{{"index":0,"id":"{call_id}",'
        f'"type":"function","function":{{"name":"{tool_name}","arguments":""}}}}]}},'
        f'"finish_reason":null}}]}}\n\n'
    ).encode()
    yield (
        f'data: {{"id":"{msg_id}","object":"chat.completion.chunk","choices":[{{"index":0,'
        f'"delta":{{"tool_calls":[{{"index":0,"function":{{"arguments":"{escaped_args}"}}}}]}},'
        f'"finish_reason":null}}]}}\n\n'
    ).encode()
    yield (
        f'data: {{"id":"{msg_id}","object":"chat.completion.chunk","choices":[{{"index":0,'
        f'"delta":{{}},"finish_reason":"tool_calls"}}]}}\n\n'
    ).encode()
    yield b'data: [DONE]\n\n'


def openai_tool_response(tool_name: str, tool_input: dict, model: str) -> dict:
    call_id = f"call_{secrets.token_hex(8)}"
    return {
        "id": f"chatcmpl-emul-{secrets.token_hex(4)}",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(tool_input)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def openai_text_response(text: str, model: str) -> dict:
    return {
        "id": f"chatcmpl-emul-{secrets.token_hex(4)}",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def openai_text_sse(text: str) -> AsyncIterator[bytes]:
    msg_id = f"chatcmpl-emul-{secrets.token_hex(4)}"
    yield (
        f'data: {{"id":"{msg_id}","object":"chat.completion.chunk","choices":[{{"index":0,'
        f'"delta":{{"role":"assistant"}},"finish_reason":null}}]}}\n\n'
    ).encode()
    chunk = 80
    for i in range(0, len(text), chunk):
        piece = json.dumps(text[i:i + chunk])[1:-1]
        yield (
            f'data: {{"id":"{msg_id}","object":"chat.completion.chunk","choices":[{{"index":0,'
            f'"delta":{{"content":"{piece}"}},"finish_reason":null}}]}}\n\n'
        ).encode()
    yield (
        f'data: {{"id":"{msg_id}","object":"chat.completion.chunk","choices":[{{"index":0,'
        f'"delta":{{}},"finish_reason":"stop"}}]}}\n\n'
    ).encode()
    yield b'data: [DONE]\n\n'
