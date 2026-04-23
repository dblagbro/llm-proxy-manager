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


def anthropic_tools_response(tool_calls: list[dict], model: str) -> dict:
    """Wave 5 #23 — emit MULTIPLE tool_use blocks for parallel tool calling."""
    content = [
        {
            "type": "tool_use",
            "id": f"toolu_{secrets.token_hex(8)}",
            "name": tc["name"],
            "input": tc.get("input", {}),
        }
        for tc in tool_calls
    ]
    return {
        "id": f"msg_emul_{secrets.token_hex(4)}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


async def anthropic_tools_sse(tool_calls: list[dict]) -> AsyncIterator[bytes]:
    """Stream MULTIPLE tool_use content blocks (one block index per tool)."""
    for idx, tc in enumerate(tool_calls):
        tool_id = f"toolu_{secrets.token_hex(8)}"
        tool_name = tc["name"]
        input_json = json.dumps(tc.get("input", {}))
        escaped = json.dumps(input_json)[1:-1]
        yield (
            f'data: {{"type":"content_block_start","index":{idx},"content_block":{{'
            f'"type":"tool_use","id":"{tool_id}","name":"{tool_name}","input":{{}}}}}}\n\n'
        ).encode()
        yield (
            f'data: {{"type":"content_block_delta","index":{idx},"delta":{{'
            f'"type":"input_json_delta","partial_json":"{escaped}"}}}}\n\n'
        ).encode()
        yield f'data: {{"type":"content_block_stop","index":{idx}}}\n\n'.encode()
    yield b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":10}}\n\n'
    yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'


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


def openai_tools_response(tool_calls: list[dict], model: str) -> dict:
    """Wave 5 #23 — emit multiple tool_calls for parallel tool calling."""
    return {
        "id": f"chatcmpl-emul-{secrets.token_hex(4)}",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{secrets.token_hex(8)}",
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("input", {})),
                        },
                    }
                    for tc in tool_calls
                ],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def openai_tools_sse(tool_calls: list[dict]) -> AsyncIterator[bytes]:
    """Stream multiple tool_calls in a single OpenAI chunk."""
    msg_id = f"chatcmpl-emul-{secrets.token_hex(4)}"
    call_headers = []
    for idx, tc in enumerate(tool_calls):
        call_id = f"call_{secrets.token_hex(8)}"
        call_headers.append({
            "index": idx, "id": call_id, "type": "function",
            "function": {"name": tc["name"], "arguments": ""},
        })
    first_chunk = {
        "id": msg_id, "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": call_headers},
                     "finish_reason": None}],
    }
    yield f"data: {json.dumps(first_chunk)}\n\n".encode()
    # Stream arguments one tool at a time
    for idx, tc in enumerate(tool_calls):
        args_json = json.dumps(tc.get("input", {}))
        delta = {
            "id": msg_id, "object": "chat.completion.chunk",
            "choices": [{"index": 0,
                         "delta": {"tool_calls": [{"index": idx, "function": {"arguments": args_json}}]},
                         "finish_reason": None}],
        }
        yield f"data: {json.dumps(delta)}\n\n".encode()
    final = {
        "id": msg_id, "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    yield f"data: {json.dumps(final)}\n\n".encode()
    yield b"data: [DONE]\n\n"


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


# ── Non-streaming Anthropic response builder ──────────────────────────────────

FINISH_TO_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}


def extract_cache_tokens(usage) -> tuple[int, int]:
    """Extract (cache_creation, cache_read) tokens from a litellm usage object.

    Works across provider SDKs: Anthropic-style fields on usage,
    OpenAI-style `prompt_tokens_details.cached_tokens`, and the
    `_response_ms._hidden_params` escape hatch litellm uses.
    """
    if not usage:
        return 0, 0
    creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    if not read:
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            read = int(getattr(details, "cached_tokens", 0) or 0)
    return creation, read


def to_anthropic_response(litellm_response) -> dict:
    choice = litellm_response.choices[0]
    finish = choice.finish_reason or "stop"
    content: list = []
    if choice.message.content:
        content.append({"type": "text", "text": choice.message.content})
    tool_calls = getattr(choice.message, "tool_calls", None) or []
    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        if not fn:
            continue
        try:
            tool_input = json.loads(getattr(fn, "arguments", "{}") or "{}")
        except (ValueError, TypeError):
            tool_input = {}
        content.append({
            "type": "tool_use",
            "id": getattr(tc, "id", None) or f"toolu_{secrets.token_hex(8)}",
            "name": getattr(fn, "name", "") or "",
            "input": tool_input,
        })
    if not content:
        content = [{"type": "text", "text": ""}]
    cache_creation, cache_read = extract_cache_tokens(litellm_response.usage)
    usage_out = {
        "input_tokens": getattr(litellm_response.usage, "prompt_tokens", 0),
        "output_tokens": getattr(litellm_response.usage, "completion_tokens", 0),
    }
    if cache_creation:
        usage_out["cache_creation_input_tokens"] = cache_creation
    if cache_read:
        usage_out["cache_read_input_tokens"] = cache_read
    return {
        "id": litellm_response.id or "msg_proxy",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": litellm_response.model or "unknown",
        "stop_reason": FINISH_TO_STOP.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": usage_out,
    }
