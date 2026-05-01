"""OpenAI Chat Completions ↔ Anthropic Messages format translation (v3.0.38).

Lets ``/v1/chat/completions`` callers reach claude-oauth providers without
client-side rewrites. DevinGPT 2026-05-01 ask: their entire stack speaks
OpenAI ChatCompletion, but the proxy's claude-oauth dispatch only supports
Anthropic Messages. v2.8.11 used to filter claude-oauth out of /v1/chat/completions
entirely; v3.0.38 routes through this translator instead.

Wire format pairs:
  OpenAI ``messages[]`` (system + user/assistant + tool/role roundtrips)
    ↔ Anthropic ``system`` + ``messages[]`` (user/assistant only,
      tool_use/tool_result inside content blocks)
  OpenAI ``tools[]`` (each {type:'function', function:{name, description, parameters}})
    ↔ Anthropic ``tools[]`` (each {name, description, input_schema})
  OpenAI ``tool_calls`` on assistant messages
    ↔ Anthropic ``tool_use`` content blocks
  OpenAI tool-result messages (role:'tool', tool_call_id, content)
    ↔ Anthropic tool_result content blocks inside a user message

Streaming: OpenAI emits delta chunks with finish_reason; Anthropic emits
content_block_start/delta/stop events. Translator subscribes to Anthropic
SSE and emits OpenAI-shape deltas.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Any, AsyncIterator, Optional


def openai_messages_to_anthropic(messages: list[dict]) -> tuple[list[dict] | str | None, list[dict]]:
    """Split OpenAI ``messages`` into Anthropic ``(system, messages)``.
    System messages collapse into the top-level ``system`` field.
    Tool messages collapse into tool_result content blocks on the next user
    message (or a synthesized one if absent)."""
    system_parts: list[str] = []
    out_messages: list[dict] = []
    pending_tool_results: list[dict] = []  # Anthropic tool_result blocks awaiting user role

    def flush_pending_tool_results() -> None:
        """Wrap any pending tool_results in a user message and emit."""
        nonlocal pending_tool_results
        if pending_tool_results:
            out_messages.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for m in messages:
        role = m.get("role")
        content = m.get("content")

        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                # OpenAI also supports content arrays
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        t = blk.get("text", "")
                        if t:
                            system_parts.append(t)
            continue

        if role == "tool":
            # tool result message — accumulate until next user message
            tcid = m.get("tool_call_id") or m.get("id") or ""
            tc_content: Any
            if isinstance(content, str):
                tc_content = content
            elif isinstance(content, list):
                # Some clients send structured content
                tc_content = content
            else:
                tc_content = json.dumps(content) if content is not None else ""
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": tcid,
                "content": tc_content,
            })
            continue

        if role == "user":
            # If tool_results are pending, prepend them to this user message
            user_blocks: list[dict] = list(pending_tool_results)
            pending_tool_results = []
            if isinstance(content, str):
                user_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict):
                        # Pass-through if Anthropic-shape; convert if OpenAI-shape
                        if blk.get("type") == "text":
                            user_blocks.append({"type": "text", "text": blk.get("text", "")})
                        elif blk.get("type") == "image_url":
                            url = (blk.get("image_url") or {}).get("url", "")
                            if url.startswith("data:"):
                                # data:image/png;base64,XXXX
                                try:
                                    media_part, b64 = url.split(",", 1)
                                    media_type = media_part.split(";")[0].split(":", 1)[1]
                                except Exception:
                                    media_type, b64 = "image/png", ""
                                user_blocks.append({
                                    "type": "image",
                                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                                })
                            else:
                                user_blocks.append({
                                    "type": "image",
                                    "source": {"type": "url", "url": url},
                                })
                        else:
                            # Already Anthropic-shape, pass through
                            user_blocks.append(blk)
            if user_blocks:
                out_messages.append({"role": "user", "content": user_blocks})
            continue

        if role == "assistant":
            flush_pending_tool_results()
            asst_blocks: list[dict] = []
            if isinstance(content, str) and content:
                asst_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        asst_blocks.append({"type": "text", "text": blk.get("text", "")})
            tool_calls = m.get("tool_calls") or []
            for tc in tool_calls:
                fn = tc.get("function") or {}
                args_raw = fn.get("arguments") or "{}"
                try:
                    tc_input = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except (ValueError, TypeError):
                    tc_input = {}
                asst_blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{secrets.token_hex(8)}",
                    "name": fn.get("name") or "",
                    "input": tc_input,
                })
            if asst_blocks:
                out_messages.append({"role": "assistant", "content": asst_blocks})
            continue

    flush_pending_tool_results()

    system_field: list[dict] | str | None
    if not system_parts:
        system_field = None
    elif len(system_parts) == 1:
        system_field = system_parts[0]
    else:
        system_field = [{"type": "text", "text": s} for s in system_parts]

    return system_field, out_messages


def openai_tools_to_anthropic(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    out: list[dict] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") and t.get("type") != "function":
            # Anthropic only knows function-style tools today
            continue
        fn = t.get("function") or {}
        out.append({
            "name": fn.get("name") or "",
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out or None


def openai_request_to_anthropic(body: dict) -> dict:
    """Translate full OpenAI Chat Completions request body → Anthropic Messages body."""
    system, messages = openai_messages_to_anthropic(body.get("messages") or [])
    out: dict[str, Any] = {
        "model": body.get("model") or "claude-sonnet-4-6",
        "max_tokens": body.get("max_tokens") or 4096,
        "messages": messages,
    }
    if system is not None:
        out["system"] = system
    if "temperature" in body:
        out["temperature"] = body["temperature"]
    if "top_p" in body:
        out["top_p"] = body["top_p"]
    if "stop" in body:
        # OpenAI stop is str | list[str], Anthropic stop_sequences is list[str]
        s = body["stop"]
        out["stop_sequences"] = [s] if isinstance(s, str) else list(s or [])
    tools = openai_tools_to_anthropic(body.get("tools"))
    if tools:
        out["tools"] = tools
        # Anthropic tool_choice: {"type":"auto"|"any"|"tool"|"none"} ;
        # OpenAI tool_choice: "none"|"auto"|"required"|{"type":"function","function":{"name"}}
        oc = body.get("tool_choice")
        if oc == "auto":
            out["tool_choice"] = {"type": "auto"}
        elif oc == "required":
            out["tool_choice"] = {"type": "any"}
        elif oc == "none":
            out["tool_choice"] = {"type": "auto"}  # Anthropic has no "none"; we emulate by not sending tools
            out.pop("tools", None)
        elif isinstance(oc, dict) and oc.get("type") == "function":
            out["tool_choice"] = {"type": "tool", "name": (oc.get("function") or {}).get("name", "")}
    return out


# Anthropic stop_reason → OpenAI finish_reason
_STOP_REASON_TO_FINISH = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


def anthropic_response_to_openai(data: dict, requested_model: str = "") -> dict:
    """Translate non-streaming Anthropic Messages response → OpenAI ChatCompletion."""
    content_blocks = data.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for blk in content_blocks:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "text":
            text_parts.append(blk.get("text", ""))
        elif blk.get("type") == "tool_use":
            tool_calls.append({
                "id": blk.get("id") or f"call_{secrets.token_hex(8)}",
                "type": "function",
                "function": {
                    "name": blk.get("name") or "",
                    "arguments": json.dumps(blk.get("input") or {}),
                },
            })
    text = "".join(text_parts)
    finish = _STOP_REASON_TO_FINISH.get(data.get("stop_reason") or "end_turn", "stop")
    if tool_calls and finish == "stop":
        finish = "tool_calls"
    msg: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    usage = data.get("usage") or {}
    return {
        "id": data.get("id") or f"chatcmpl-{secrets.token_hex(8)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model or data.get("model") or "unknown",
        "choices": [{
            "index": 0,
            "message": msg,
            "finish_reason": finish,
        }],
        "usage": {
            "prompt_tokens": int(usage.get("input_tokens") or 0),
            "completion_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
        },
    }


async def stream_anthropic_to_openai_sse(
    anthropic_sse: AsyncIterator[bytes],
    requested_model: str,
) -> AsyncIterator[bytes]:
    """Read an Anthropic SSE stream and re-emit as OpenAI ChatCompletion SSE.

    Anthropic events we care about:
      message_start          → emit OpenAI chunk with role=assistant + initial usage
      content_block_start    → if text, nothing; if tool_use, emit tool_calls with id+name
      content_block_delta    → text_delta → content delta; input_json_delta → tool_calls.arguments delta
      content_block_stop     → no-op
      message_delta          → carries stop_reason + final usage
      message_stop           → emit final chunk with finish_reason + [DONE]
    """
    chunk_id = f"chatcmpl-{secrets.token_hex(8)}"
    created = int(time.time())
    base_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": requested_model,
    }

    finish_reason: Optional[str] = None
    tool_index_by_block: dict[int, int] = {}  # Anthropic block index → OpenAI tool_calls index
    next_tool_index = 0

    def emit_chunk(delta: dict, finish: Optional[str] = None) -> bytes:
        c = {**base_chunk, "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish,
        }]}
        return f"data: {json.dumps(c)}\n\n".encode()

    # Initial role=assistant chunk
    yield emit_chunk({"role": "assistant", "content": ""}, None)

    buffer = b""
    async for chunk in anthropic_sse:
        buffer += chunk
        while b"\n\n" in buffer:
            event, buffer = buffer.split(b"\n\n", 1)
            # SSE event format: lines starting with "event:" / "data:"
            data_line = b""
            for line in event.split(b"\n"):
                if line.startswith(b"data: "):
                    data_line = line[6:]
                    break
            if not data_line:
                continue
            try:
                evt = json.loads(data_line.decode())
            except (ValueError, UnicodeDecodeError):
                continue
            etype = evt.get("type")
            if etype == "content_block_start":
                blk = evt.get("content_block") or {}
                idx = evt.get("index", 0)
                if blk.get("type") == "tool_use":
                    tool_idx = next_tool_index
                    tool_index_by_block[idx] = tool_idx
                    next_tool_index += 1
                    yield emit_chunk({"tool_calls": [{
                        "index": tool_idx,
                        "id": blk.get("id") or f"call_{secrets.token_hex(8)}",
                        "type": "function",
                        "function": {"name": blk.get("name") or "", "arguments": ""},
                    }]})
            elif etype == "content_block_delta":
                idx = evt.get("index", 0)
                d = evt.get("delta") or {}
                if d.get("type") == "text_delta":
                    yield emit_chunk({"content": d.get("text") or ""})
                elif d.get("type") == "input_json_delta":
                    tool_idx = tool_index_by_block.get(idx)
                    if tool_idx is not None:
                        yield emit_chunk({"tool_calls": [{
                            "index": tool_idx,
                            "function": {"arguments": d.get("partial_json") or ""},
                        }]})
            elif etype == "message_delta":
                stop = (evt.get("delta") or {}).get("stop_reason")
                if stop:
                    finish_reason = _STOP_REASON_TO_FINISH.get(stop, "stop")
            elif etype == "message_stop":
                # Final chunk + [DONE]
                yield emit_chunk({}, finish_reason or "stop")
                yield b"data: [DONE]\n\n"
                return
            # Other events (message_start, ping, content_block_stop) are no-ops
    # Stream ended without explicit message_stop
    yield emit_chunk({}, finish_reason or "stop")
    yield b"data: [DONE]\n\n"
