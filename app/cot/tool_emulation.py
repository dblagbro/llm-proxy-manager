"""
Tool-use emulation — proxy-side injection for providers without native function calling.

When a request includes tools but the selected provider doesn't natively support them,
this module:
  1. Converts tool schemas into a system prompt the model can follow
  2. Normalizes prior tool_use / tool_result message history into plain text
  3. Parses the model response for <tool_call> markers
  4. Formats synthetic tool_use SSE / JSON blocks for the client in both
     Anthropic and OpenAI wire formats
"""
from __future__ import annotations

import json
import re
import secrets
from typing import AsyncIterator

import litellm

_TOOL_CALL_RE = re.compile(
    r"<(?:tool_call|tool_code|function_call|tool_use)>\s*(\{.*?\})\s*</(?:tool_call|tool_code|function_call|tool_use)>",
    re.DOTALL,
)

_TOOL_PROMPT = """\
You have access to the following tools. When you want to call a tool, output ONLY this \
exact format and nothing else:

<tool_call>
{{"name": "TOOL_NAME", "input": {{...arguments as JSON...}}}}
</tool_call>

After the tool executes you will receive the result and can continue. \
If no tool call is needed, respond normally without the <tool_call> tags.

## Available Tools

{descriptions}"""


# ── Schema → system prompt ────────────────────────────────────────────────────

def _describe_anthropic(tool: dict) -> str:
    name = tool.get("name", "unknown")
    desc = tool.get("description", "No description.")
    schema = tool.get("input_schema", {})
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    lines = [f"### {name}", desc]
    if props:
        lines.append("Parameters:")
        for pname, pdef in props.items():
            typ = pdef.get("type", "any")
            pdesc = pdef.get("description", "")
            req = " (required)" if pname in required else ""
            lines.append(f"  - {pname} ({typ}{req}): {pdesc}")
    return "\n".join(lines)


def _describe_openai(tool: dict) -> str:
    func = tool.get("function", tool)
    name = func.get("name", "unknown")
    desc = func.get("description", "No description.")
    params = func.get("parameters", {})
    props = params.get("properties", {})
    required = set(params.get("required", []))
    lines = [f"### {name}", desc]
    if props:
        lines.append("Parameters:")
        for pname, pdef in props.items():
            typ = pdef.get("type", "any")
            pdesc = pdef.get("description", "")
            req = " (required)" if pname in required else ""
            lines.append(f"  - {pname} ({typ}{req}): {pdesc}")
    return "\n".join(lines)


def build_anthropic_tool_prompt(tools: list[dict]) -> str:
    descriptions = "\n\n".join(_describe_anthropic(t) for t in tools)
    return _TOOL_PROMPT.format(descriptions=descriptions)


def build_openai_tool_prompt(tools: list[dict]) -> str:
    descriptions = "\n\n".join(_describe_openai(t) for t in tools)
    return _TOOL_PROMPT.format(descriptions=descriptions)


# ── Message normalisation (for multi-turn tool use) ───────────────────────────

def normalize_anthropic_messages(messages: list[dict]) -> list[dict]:
    """
    Convert Anthropic-format messages containing tool_use / tool_result blocks
    into plain-text equivalents a non-native model can follow.
    """
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        parts: list[str] = []
        for block in content:
            btype = block.get("type", "text")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                parts.append(f"<tool_call>\n{json.dumps({'name': name, 'input': inp})}\n</tool_call>")
            elif btype == "tool_result":
                tid = block.get("tool_use_id", "")
                result = block.get("content", "")
                if isinstance(result, list):
                    result = " ".join(b.get("text", "") for b in result if b.get("type") == "text")
                parts.append(f'<tool_result tool_use_id="{tid}">\n{result}\n</tool_result>')
        normalized = "\n".join(parts).strip()
        if normalized:
            out.append({"role": role, "content": normalized})
    return out


def normalize_openai_messages(messages: list[dict]) -> list[dict]:
    """
    Convert OpenAI-format messages containing tool_calls / role=tool
    into plain-text equivalents a non-native model can follow.
    """
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        if role == "tool":
            tid = msg.get("tool_call_id", "")
            out.append({
                "role": "user",
                "content": f'<tool_result tool_call_id="{tid}">\n{content}\n</tool_result>',
            })
            continue
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            parts = [content] if content else []
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except (json.JSONDecodeError, ValueError):
                    args = {}
                parts.append(f"<tool_call>\n{json.dumps({'name': name, 'input': args})}\n</tool_call>")
            out.append({"role": "assistant", "content": "\n".join(parts).strip()})
            continue
        out.append({"role": role, "content": content})
    return out


# ── Response parser ───────────────────────────────────────────────────────────

def parse_tool_call(text: str) -> dict | None:
    """
    Extract the first tool-call block from a model response.
    Handles <tool_call>, <tool_code>, <function_call>, and <tool_use> tags.
    Normalizes alternate field names (function_name→name, parameters/arguments/args→input).
    Returns {"name": str, "input": dict} or None.
    """
    m = _TOOL_CALL_RE.search(text)
    if not m:
        return None
    try:
        payload = json.loads(m.group(1))
        # Normalize name field
        for alt_name in ("function_name", "tool_name"):
            if alt_name in payload and "name" not in payload:
                payload["name"] = payload.pop(alt_name)
        if "name" not in payload:
            return None
        # Normalize input field
        if "input" not in payload:
            for alt_input in ("parameters", "arguments", "args", "kwargs"):
                if alt_input in payload:
                    payload["input"] = payload.pop(alt_input)
                    break
        payload.setdefault("input", {})
        return payload
    except (json.JSONDecodeError, ValueError):
        return None


# ── Internal LLM call ─────────────────────────────────────────────────────────

async def call_with_tool_prompt(
    model: str,
    messages: list[dict],
    system: str | None,
    extra: dict,
) -> str:
    """Non-streaming litellm call; returns the assistant text content."""
    kwargs = {k: v for k, v in extra.items() if k not in ("max_tokens", "system", "tools", "stream")}
    msgs = list(messages)
    if system:
        msgs = [{"role": "system", "content": system}] + msgs
    resp = await litellm.acompletion(
        model=model,
        messages=msgs,
        stream=False,
        max_tokens=extra.get("max_tokens", 1024),
        **kwargs,
    )
    choice = resp.choices[0]
    return choice.message.content or ""


# ── Anthropic SSE generators ──────────────────────────────────────────────────

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


# ── OpenAI SSE generators ─────────────────────────────────────────────────────

async def openai_tool_sse(tool_name: str, tool_input: dict) -> AsyncIterator[bytes]:
    call_id = f"call_{secrets.token_hex(8)}"
    msg_id = f"chatcmpl-emul-{secrets.token_hex(4)}"
    args_json = json.dumps(tool_input)
    escaped_args = json.dumps(args_json)[1:-1]

    # Role + tool call name
    yield (
        f'data: {{"id":"{msg_id}","object":"chat.completion.chunk","choices":[{{"index":0,'
        f'"delta":{{"role":"assistant","tool_calls":[{{"index":0,"id":"{call_id}",'
        f'"type":"function","function":{{"name":"{tool_name}","arguments":""}}}}]}},'
        f'"finish_reason":null}}]}}\n\n'
    ).encode()
    # Arguments delta
    yield (
        f'data: {{"id":"{msg_id}","object":"chat.completion.chunk","choices":[{{"index":0,'
        f'"delta":{{"tool_calls":[{{"index":0,"function":{{"arguments":"{escaped_args}"}}}}]}},'
        f'"finish_reason":null}}]}}\n\n'
    ).encode()
    # Finish
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
