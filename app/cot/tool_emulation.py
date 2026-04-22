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


