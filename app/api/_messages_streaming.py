"""
Tail functions for /v1/messages (Anthropic endpoint).

Extracted from ``app/api/messages.py`` in the 2026-04-23 refactor so the
POST handler can stay focused on routing + response assembly.

Functions:
  _stream_cot_anthropic   — pass-through around run_cot_pipeline + metrics
  _stream_anthropic       — the main Anthropic streaming translator
  _webhook_completion_anthropic — fire-and-forget async delivery

Behavior is unchanged from the pre-extraction version; this file is a
pure move with import adjustments.
"""
from __future__ import annotations

import json
import time
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.cot.pipeline import run_cot_pipeline
from app.cot.sse import FINISH_TO_STOP, to_anthropic_response, extract_cache_tokens
from app.routing.retry import acompletion_with_retry
from app.monitoring.helpers import record_outcome
from app.cache import maybe_store
from app.api.webhook import post_webhook


async def _stream_cot_anthropic(
    model: str,
    messages: list,
    session_id: str | None,
    extra: dict,
    max_iterations: int | None,
    provider_id: str,
    db: AsyncSession,
    key_record_id: str,
    force_verify: bool | None = None,
    critique_model: str | None = None,
    critique_kwargs: dict | None = None,
    samples: int = 1,
    task_branch: str | None = None,
) -> AsyncIterator[bytes]:
    """Pass-through wrapper around run_cot_pipeline; records metrics after completion."""
    import json as _json
    in_tok = out_tok = 0
    cache_creation = cache_read = 0
    t0 = time.monotonic()
    try:
        async for chunk in run_cot_pipeline(
            model, messages, session_id, extra, max_iterations, force_verify,
            critique_model=critique_model, critique_kwargs=critique_kwargs,
            samples=samples, task_branch=task_branch,
        ):
            yield chunk
            line = chunk.decode(errors="ignore").strip()
            if line.startswith("data: "):
                try:
                    evt = _json.loads(line[6:])
                    if evt.get("type") == "message_delta":
                        usage = evt.get("usage", {})
                        in_tok = usage.get("input_tokens", in_tok)
                        out_tok = usage.get("output_tokens", out_tok)
                        cache_creation = usage.get("cache_creation_input_tokens", cache_creation) or cache_creation
                        cache_read = usage.get("cache_read_input_tokens", cache_read) or cache_read
                except (ValueError, KeyError):
                    pass
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id,
                             cache_creation=cache_creation, cache_read=cache_read)
    except Exception as e:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"type": "error", "error": {"message": str(e)}}).encode() + b'\n\n')
        yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'


async def _stream_anthropic(
    model: str, messages: list, extra: dict, provider_id: str,
    db: AsyncSession, key_record_id: str, t0: float, budget_total: int = 0,
    cache_decision=None,
) -> AsyncIterator[bytes]:
    try:
        response = await acompletion_with_retry(model=model, messages=messages, stream=True, **extra)
        index = 0
        text_started = False
        tool_started = False
        finish_reason = "stop"
        input_tokens = 0
        output_tokens = 0
        cache_creation = 0
        cache_read = 0
        streamed_chars = 0
        tool_id: str = ""
        tool_name: str = ""
        ttft_ms: float = 0.0
        full_text_buf: list[str] = []

        yield (
            f'data: {{"type":"message_start","message":{{"id":"msg_proxy","type":"message",'
            f'"role":"assistant","content":[],"model":"{model}",'
            f'"stop_reason":null,"stop_sequence":null,'
            f'"usage":{{"input_tokens":0,"output_tokens":0}}}}}}\n\n'
        ).encode()

        async for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            if hasattr(chunk, "usage") and chunk.usage:
                input_tokens = getattr(chunk.usage, "prompt_tokens", input_tokens)
                output_tokens = getattr(chunk.usage, "completion_tokens", output_tokens)
                c_create, c_read = extract_cache_tokens(chunk.usage)
                if c_create:
                    cache_creation = c_create
                if c_read:
                    cache_read = c_read

            # Tool call streaming
            tool_calls = getattr(delta, "tool_calls", None) or []
            for tc_delta in tool_calls:
                fn = getattr(tc_delta, "function", None)
                if not fn:
                    continue
                if not tool_started:
                    if not ttft_ms:
                        ttft_ms = (time.monotonic() - t0) * 1000
                    tool_id = getattr(tc_delta, "id", "") or f"toolu_{id(tc_delta)}"
                    tool_name = getattr(fn, "name", "") or ""
                    yield (
                        f'data: {{"type":"content_block_start","index":{index},'
                        f'"content_block":{{"type":"tool_use","id":"{tool_id}",'
                        f'"name":"{tool_name}","input":{{}}}}}}\n\n'
                    ).encode()
                    tool_started = True
                args_fragment = getattr(fn, "arguments", "") or ""
                if args_fragment:
                    escaped = json.dumps(args_fragment)[1:-1]
                    yield (
                        f'data: {{"type":"content_block_delta","index":{index},'
                        f'"delta":{{"type":"input_json_delta","partial_json":"{escaped}"}}}}\n\n'
                    ).encode()

            # Text streaming
            content = getattr(delta, "content", None) or ""
            if not text_started and content:
                if not ttft_ms:
                    ttft_ms = (time.monotonic() - t0) * 1000
                yield f'data: {{"type":"content_block_start","index":{index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()
                text_started = True
            if content:
                streamed_chars += len(content)
                full_text_buf.append(content)
                escaped = json.dumps(content)[1:-1]
                yield f'data: {{"type":"content_block_delta","index":{index},"delta":{{"type":"text_delta","text":"{escaped}"}}}}\n\n'.encode()

        if text_started or tool_started:
            yield f'data: {{"type":"content_block_stop","index":{index}}}\n\n'.encode()

        if output_tokens == 0 and streamed_chars > 0:
            output_tokens = max(1, streamed_chars // 4)

        stop_reason = FINISH_TO_STOP.get(finish_reason, "end_turn")
        usage_parts = [f'"output_tokens":{output_tokens}']
        if cache_creation:
            usage_parts.append(f'"cache_creation_input_tokens":{cache_creation}')
        if cache_read:
            usage_parts.append(f'"cache_read_input_tokens":{cache_read}')
        yield (
            f'data: {{"type":"message_delta","delta":{{"stop_reason":"{stop_reason}",'
            f'"stop_sequence":null}},"usage":{{{",".join(usage_parts)}}}}}\n\n'
        ).encode()
        if budget_total > 0:
            remaining = max(0, budget_total - output_tokens)
            yield (
                f'event: budget\ndata: {{"remaining":{remaining},'
                f'"used":{output_tokens},"total":{budget_total}}}\n\n'
            ).encode()
        yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=input_tokens, out_tok=output_tokens, t0=t0,
                             key_record_id=key_record_id, ttft_ms=ttft_ms,
                             cache_creation=cache_creation, cache_read=cache_read)
        if cache_decision is not None and cache_decision.eligible:
            try:
                await maybe_store(cache_decision, "".join(full_text_buf))
            except Exception:
                pass
    except Exception as e:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"type": "error", "error": {"message": str(e)}}).encode() + b'\n\n')
        yield b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'


async def _webhook_completion_anthropic(
    webhook_url: str,
    model: str,
    messages: list,
    extra: dict,
    provider_id: str,
    db: AsyncSession,
    key_record_id: str,
) -> None:
    """Run a non-streaming completion and POST the result to webhook_url."""
    t0 = time.monotonic()
    try:
        result = await acompletion_with_retry(model=model, messages=messages, stream=False, **extra)
        in_tok = getattr(result.usage, "prompt_tokens", 0)
        out_tok = getattr(result.usage, "completion_tokens", 0)
        cache_creation, cache_read = extract_cache_tokens(result.usage)
        await record_outcome(db, provider_id, model, success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id,
                             cache_creation=cache_creation, cache_read=cache_read)
        await post_webhook(webhook_url, {
            "provider": provider_id,
            "model": model,
            "response": to_anthropic_response(result),
        })
    except Exception as exc:
        await record_outcome(db, provider_id, model, success=False,
                             key_record_id=key_record_id, error_str=str(exc))
        await post_webhook(webhook_url, {"error": str(exc), "model": model})
