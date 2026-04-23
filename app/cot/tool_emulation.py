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

import litellm

_TOOL_CALL_RE = re.compile(
    r"<(?:tool_call|tool_code|function_call|tool_use)>\s*(\{.*?\})\s*</(?:tool_call|tool_code|function_call|tool_use)>",
    re.DOTALL,
)

_TOOL_PROMPT = """\
You have access to the following tools. When you want to call a tool, output ONLY \
tool_call blocks — no prose between or around them:

<tool_call>
{{"name": "TOOL_NAME", "input": {{...arguments as JSON...}}}}
</tool_call>

PARALLEL TOOL USE: If you need to call MULTIPLE independent tools, emit MULTIPLE \
<tool_call> blocks in sequence — one block per tool. The tools will run in parallel \
and all results will be returned together in the next turn.

After the tool(s) execute you will receive the result(s) and can continue. \
If no tool call is needed, respond normally without any <tool_call> tags.

## Available Tools

{descriptions}"""


_TOOL_PROMPT_SERIAL = """\
You have access to the following tools. When you want to call a tool, output ONLY \
this exact format and nothing else:

<tool_call>
{{"name": "TOOL_NAME", "input": {{...arguments as JSON...}}}}
</tool_call>

Only ONE tool call per turn — the client has opted out of parallel tool calls.

After the tool executes you will receive the result and can continue. \
If no tool call is needed, respond normally without the <tool_call> tags.

## Available Tools

{descriptions}"""


# ── Schema → system prompt ────────────────────────────────────────────────────

def _render_tool_description(name: str, desc: str, props: dict, required: set) -> str:
    lines = [f"### {name}", desc]
    if props:
        lines.append("Parameters:")
        for pname, pdef in props.items():
            typ = pdef.get("type", "any")
            pdesc = pdef.get("description", "")
            req = " (required)" if pname in required else ""
            lines.append(f"  - {pname} ({typ}{req}): {pdesc}")
    return "\n".join(lines)


def _describe_anthropic(tool: dict) -> str:
    schema = tool.get("input_schema", {})
    return _render_tool_description(
        tool.get("name", "unknown"),
        tool.get("description", "No description."),
        schema.get("properties", {}),
        set(schema.get("required", [])),
    )


def _describe_openai(tool: dict) -> str:
    func = tool.get("function", tool)
    params = func.get("parameters", {})
    return _render_tool_description(
        func.get("name", "unknown"),
        func.get("description", "No description."),
        params.get("properties", {}),
        set(params.get("required", [])),
    )


def build_anthropic_tool_prompt(tools: list[dict], allow_parallel: bool = True) -> str:
    descriptions = "\n\n".join(_describe_anthropic(t) for t in tools)
    template = _TOOL_PROMPT if allow_parallel else _TOOL_PROMPT_SERIAL
    return template.format(descriptions=descriptions)


def build_openai_tool_prompt(tools: list[dict], allow_parallel: bool = True) -> str:
    descriptions = "\n\n".join(_describe_openai(t) for t in tools)
    template = _TOOL_PROMPT if allow_parallel else _TOOL_PROMPT_SERIAL
    return template.format(descriptions=descriptions)


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

def _normalize_tool_payload(payload: dict) -> dict | None:
    for alt_name in ("function_name", "tool_name"):
        if alt_name in payload and "name" not in payload:
            payload["name"] = payload.pop(alt_name)
    if "name" not in payload:
        return None
    if "input" not in payload:
        for alt_input in ("parameters", "arguments", "args", "kwargs"):
            if alt_input in payload:
                payload["input"] = payload.pop(alt_input)
                break
    payload.setdefault("input", {})
    return payload


def parse_tool_calls(text: str) -> list[dict]:
    """Wave 5 #23 — Extract ALL tool-call blocks from a model response.

    Returns a possibly-empty list of {"name": str, "input": dict} dicts,
    preserving emission order. Used for parallel-tool-call emulation.
    """
    out: list[dict] = []
    for m in _TOOL_CALL_RE.finditer(text):
        try:
            payload = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        norm = _normalize_tool_payload(payload)
        if norm is not None:
            out.append(norm)
    return out


def parse_tool_call(text: str) -> dict | None:
    """Backward-compatible single-tool-call extractor (returns the FIRST)."""
    calls = parse_tool_calls(text)
    return calls[0] if calls else None


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


