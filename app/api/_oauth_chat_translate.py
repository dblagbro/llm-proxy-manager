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


# ── v3.9.1 (#269 Fix B) — Anthropic → OpenAI reverse direction ─────
#
# When an Anthropic-shape /v1/messages request cross-family-falls back
# to an OpenAI-shape provider (gpt-4o via OpenRouter, etc), we need to
# convert the body. Without this, the upstream returns "Invalid user
# message at index N" 400s because OpenAI doesn't recognize Anthropic's
# tool_use / tool_result content blocks.
#
# Companion to ``openai_request_to_anthropic`` above.


_EMPTY_TOOL_RESULT_PLACEHOLDER = "(no output)"
# v3.9.16 (P3a) — index-preserving placeholder for user messages that
# would otherwise be empty or drop. Two failure modes the placeholder
# closes:
#   1. ``{role: "user", content: ""}`` — OpenAI rejects empty user
#      content with "Invalid user message at index N"
#   2. ``{role: "user", content: []}`` or ``[empty-blocks-only]`` —
#      _anthropic_blocks_to_openai_message_parts returned [], silently
#      dropping the message and SHIFTING indexing for every message
#      that follows. The error from OpenAI then names a different
#      index than the message that was actually malformed.
# OpenRouter 86% failure rate post-v3.9.1 was traced to one of these
# two patterns.
_EMPTY_USER_CONTENT_PLACEHOLDER = "(no input)"
# v3.10.12 BUG-028(a) — an assistant turn that produced neither text nor
# tool_use (e.g. only `thinking` blocks, which drop in cross-family
# translation) would emit {role:assistant, content:null} with no
# tool_calls, which OpenAI rejects. Emit this placeholder instead.
_EMPTY_ASSISTANT_CONTENT_PLACEHOLDER = "(no content)"


def _tool_result_content_to_str(content: Any) -> str:
    """Anthropic tool_result.content can be: str, None, or a list of
    content blocks (each {type: text|image, ...}). OpenAI's role:tool
    message content must be a non-empty string."""
    if content is None or content == "":
        return _EMPTY_TOOL_RESULT_PLACEHOLDER
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "text":
                t = blk.get("text") or ""
                if t:
                    parts.append(t)
            elif blk.get("type") == "image":
                # v3.10.14 BUG-033 — OpenAI tool-role messages cannot
                # carry image content. Emit a descriptive marker (with
                # the media type) so the dropped image is *visible* to
                # the caller, not silently flattened to "[image]". Full
                # preservation would require promoting the tool_result
                # to a user-message image part — tracked separately.
                src = blk.get("source") or {}
                media = src.get("media_type") or "image"
                parts.append(
                    f"[image omitted: {media} — OpenAI tool-role "
                    f"messages cannot carry image content]"
                )
        joined = "\n".join(parts).strip()
        return joined or _EMPTY_TOOL_RESULT_PLACEHOLDER
    # Unknown shape — coerce to str so we never return empty.
    s = str(content).strip()
    return s or _EMPTY_TOOL_RESULT_PLACEHOLDER


def _anthropic_blocks_to_openai_message_parts(
    role: str, blocks: list,
    known_tool_use_ids: Optional[set] = None,
) -> list[dict]:
    """Convert one Anthropic message's content blocks (when the message
    is list-shaped) into one or more OpenAI messages, preserving order.

    ``known_tool_use_ids`` (when provided) is the set of every tool_use
    id declared by an assistant turn in the whole conversation. A
    tool_result referencing none of them is "orphaned" and emitted as
    plain user text rather than a dangling ``role:"tool"`` message — see
    the user branch below.

    Returns a list of OpenAI-shape messages.
    """
    out: list[dict] = []

    if role == "assistant":
        # Collect text + tool_use blocks into a single assistant message
        # with optional ``content`` (text) + ``tool_calls`` (function calls).
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            t = blk.get("type")
            if t == "text":
                txt = blk.get("text") or ""
                if txt:
                    text_parts.append(txt)
            elif t == "tool_use":
                tool_calls.append({
                    "id": blk.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": blk.get("name") or "",
                        "arguments": json.dumps(blk.get("input") or {}),
                    },
                })
            # Other block types (thinking, etc.) are dropped for cross-
            # family; OpenAI has no equivalent.
        msg: dict[str, Any] = {"role": "assistant"}
        if text_parts:
            msg["content"] = "\n".join(text_parts)
        elif tool_calls:
            msg["content"] = None  # OpenAI spec: null allowed when tool_calls present
        else:
            # v3.10.12 BUG-028(a): neither text nor tool_use — content:null
            # with no tool_calls is rejected by OpenAI; use a placeholder.
            msg["content"] = _EMPTY_ASSISTANT_CONTENT_PLACEHOLDER
        if tool_calls:
            msg["tool_calls"] = tool_calls
        out.append(msg)
        return out

    if role == "user":
        # User messages can mix text + tool_result blocks. OpenAI splits
        # these: each tool_result becomes a ``role:tool`` message;
        # remaining text collapses into a single ``role:user`` message
        # AFTER the tool replies (per OpenAI's "tool-results-come-first"
        # convention when an assistant has just emitted tool_calls).
        tool_msgs: list[dict] = []
        text_parts: list[str] = []
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            t = blk.get("type")
            if t == "tool_result":
                tcid = blk.get("tool_use_id") or ""
                # v3.10.0 — a tool_result that references no tool_use
                # declared by any assistant turn (a conversation window
                # that begins mid-tool-exchange) has no assistant
                # tool_call to attach to. OpenAI rejects a dangling
                # role:"tool" message ("Invalid user message at index
                # N"), so emit the orphan as plain user text instead.
                if known_tool_use_ids is not None and tcid not in known_tool_use_ids:
                    text_parts.append(
                        "[tool result] "
                        + _tool_result_content_to_str(blk.get("content"))
                    )
                else:
                    tool_msgs.append({
                        "role": "tool",
                        "tool_call_id": tcid,
                        "content": _tool_result_content_to_str(blk.get("content")),
                    })
            elif t == "text":
                txt = blk.get("text") or ""
                if txt:
                    text_parts.append(txt)
            elif t == "image":
                # Carry image content through as an OpenAI image_url part.
                src = blk.get("source") or {}
                if src.get("type") == "base64":
                    media = src.get("media_type") or "image/jpeg"
                    data = src.get("data") or ""
                    text_parts.append(f"[image:{media};base64,{data[:40]}…]")
                else:
                    text_parts.append("[image]")
        # Emit tool messages first (they correspond to the preceding
        # assistant's tool_calls), then the user text if any.
        out.extend(tool_msgs)
        if text_parts:
            out.append({"role": "user", "content": "\n".join(text_parts)})
        # v3.9.16 — if the user message had only tool_result blocks
        # (no text or image), tool_msgs already carries the substance.
        # If it had NO blocks at all (or all-empty blocks), out is
        # empty here — emit an index-preserving placeholder so we don't
        # shift downstream message indices and confuse OpenAI's
        # "Invalid user message at index N" diagnostic.
        if not out:
            out.append({
                "role": "user",
                "content": _EMPTY_USER_CONTENT_PLACEHOLDER,
            })
        return out

    # Other roles (system handled separately above): pass through with
    # best-effort string coercion.
    coerced = "\n".join(
        (b.get("text") or "") for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    )
    out.append({"role": role, "content": coerced})
    return out


def anthropic_messages_to_openai(
    body_messages: list[dict],
    body_system: Any = None,
) -> list[dict]:
    """Translate Anthropic ``messages[]`` + top-level ``system`` field
    into OpenAI ChatCompletion ``messages[]`` shape.

    Handles:
    - ``system`` (str or list-of-text-blocks) prepended as role:system
    - per-message: string content passes through as-is
    - per-message: list-content with tool_use/tool_result/text/image
      blocks → split into multiple OpenAI messages in order
    """
    out: list[dict] = []

    # System field → leading role:system message
    if body_system is not None:
        if isinstance(body_system, str) and body_system:
            out.append({"role": "system", "content": body_system})
        elif isinstance(body_system, list):
            parts = [
                (b.get("text") or "") for b in body_system
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "\n".join(p for p in parts if p)
            if joined:
                out.append({"role": "system", "content": joined})

    # v3.10.12 BUG-028(b) — a tool_result is a valid OpenAI role:"tool"
    # message only if it answers the IMMEDIATELY preceding assistant
    # turn's tool_calls. Track that turn's tool_use ids and pass them to
    # the block translator; a tool_result matching no adjacent assistant
    # call (orphaned OR misordered / cross-turn) degrades to plain user
    # text instead of producing an OpenAI 400. (Replaces the v3.10.0
    # global pre-scan, which only caught fully-orphaned ids.)
    prev_assistant_tool_ids: set = set()
    for m in body_messages or ():
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content")

        # tool_use ids declared by THIS turn if it is an assistant
        # message — the adjacency set the next user message answers.
        this_assistant_ids: set = set()
        if role == "assistant" and isinstance(content, list):
            for blk in content:
                if (isinstance(blk, dict) and blk.get("type") == "tool_use"
                        and blk.get("id")):
                    this_assistant_ids.add(blk["id"])

        if isinstance(content, str):
            # Plain text message — straight passthrough.
            # v3.9.16 — empty user-string content was an OpenRouter
            # rejection case. Substitute placeholder so the message
            # passes OpenAI's "user message at index N" validation
            # without shifting indices.
            if role == "user" and content == "":
                out.append({"role": role, "content": _EMPTY_USER_CONTENT_PLACEHOLDER})
            else:
                out.append({"role": role, "content": content})
        elif isinstance(content, list):
            out.extend(_anthropic_blocks_to_openai_message_parts(
                role, content,
                prev_assistant_tool_ids if role == "user" else None,
            ))
        else:
            # v3.9.16 — unknown content shape: placeholder for user role
            # (empty string would 400 on OpenAI); empty string for other
            # roles (assistant accepts null/empty; system can be empty).
            if role == "user":
                out.append({"role": role, "content": _EMPTY_USER_CONTENT_PLACEHOLDER})
            else:
                out.append({"role": role, "content": ""})

        prev_assistant_tool_ids = this_assistant_ids if role == "assistant" else set()

    return out


def anthropic_tools_to_openai(tools: list[dict] | None) -> list[dict] | None:
    """Inverse of ``openai_tools_to_anthropic``. Anthropic tools are
    ``{name, description, input_schema}``; OpenAI expects
    ``{type:'function', function:{name, description, parameters}}``."""
    if not tools:
        return None
    out: list[dict] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return out or None


def anthropic_to_openai_body(body: dict) -> dict:
    """Translate a full Anthropic ``/v1/messages`` request body into the
    shape an OpenAI Chat Completions upstream expects.

    Preserves: ``model``, ``temperature``, ``top_p``, ``stop_sequences``,
    ``max_tokens``. Converts ``system``+``messages`` via
    ``anthropic_messages_to_openai`` and ``tools`` via
    ``anthropic_tools_to_openai``.

    Drops Anthropic-specific fields with no OpenAI equivalent (``thinking``,
    ``metadata``, ``anthropic_version``) so litellm doesn't pass them
    through and trigger upstream 4xx.
    """
    if not isinstance(body, dict):
        return body
    out: dict[str, Any] = {}
    if "model" in body:
        out["model"] = body["model"]
    out["messages"] = anthropic_messages_to_openai(
        body.get("messages") or [],
        body_system=body.get("system"),
    )
    if "max_tokens" in body:
        out["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        out["temperature"] = body["temperature"]
    if "top_p" in body:
        out["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        # OpenAI accepts list or single string for stop.
        out["stop"] = body["stop_sequences"]
    if "stream" in body:
        out["stream"] = body["stream"]
    tools = anthropic_tools_to_openai(body.get("tools"))
    if tools:
        out["tools"] = tools
        # tool_choice translation
        tc = body.get("tool_choice")
        if isinstance(tc, dict):
            tc_type = tc.get("type")
            if tc_type == "auto":
                out["tool_choice"] = "auto"
            elif tc_type == "any":
                out["tool_choice"] = "required"
            elif tc_type == "tool" and tc.get("name"):
                out["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tc["name"]},
                }
    return out
