"""
Tail functions for /v1/chat/completions (OpenAI endpoint).

Extracted from ``app/api/completions.py`` in the 2026-04-23 refactor so
the POST handler can stay focused on routing + response assembly.

Functions:
  _stream_cot_openai           — run CoT pipeline, re-emit as OpenAI SSE
  _stream_openai               — the main OpenAI streaming pass-through
  _webhook_completion_openai   — fire-and-forget async delivery

Behavior is unchanged from the pre-extraction version; this file is a
pure move with import adjustments.
"""
from __future__ import annotations

import json
import time
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.cot.pipeline import run_cot_pipeline
from app.routing.retry import acompletion_with_retry
from app.monitoring.helpers import record_outcome
from app.cache.middleware import maybe_store
from app.api.webhook import post_webhook


async def _stream_cot_openai(
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
    # v5.0.0 — compliance substitution disclosure (decision 15 + 28).
    compliance_disclosure: dict | None = None,
    accept_compliance_events: bool = False,
) -> AsyncIterator[bytes]:
    """
    Run the CoT-E pipeline and re-emit as OpenAI-format SSE chunks.
    Thinking blocks are collected and prepended as <thinking>…</thinking>
    text so callers can strip or display them.
    """
    thinking_buf: list[str] = []
    text_buf: list[str] = []
    in_thinking = False
    in_text = False
    in_tok = out_tok = 0
    t0 = time.monotonic()

    try:
        async for raw in run_cot_pipeline(
            model, messages, session_id, extra, max_iterations, force_verify,
            critique_model=critique_model, critique_kwargs=critique_kwargs,
            samples=samples, task_branch=task_branch,
        ):
            line = raw.decode(errors="ignore").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload in ("[DONE]", ""):
                continue
            try:
                evt = json.loads(payload)
            except ValueError:
                continue

            t = evt.get("type", "")
            if t == "content_block_start":
                block_type = evt.get("content_block", {}).get("type", "")
                in_thinking = block_type == "thinking"
                in_text = block_type == "text"
            elif t == "content_block_delta":
                delta = evt.get("delta", {})
                if in_thinking:
                    thinking_buf.append(delta.get("thinking", ""))
                elif in_text:
                    text_buf.append(delta.get("text", ""))
            elif t == "content_block_stop":
                in_thinking = False
                in_text = False
            elif t == "message_delta":
                usage = evt.get("usage", {})
                in_tok = usage.get("input_tokens", in_tok)
                out_tok = usage.get("output_tokens", out_tok)

        thinking_text = "".join(thinking_buf).strip()
        answer_text = "".join(text_buf).strip()
        full_text = (
            f"<thinking>\n{thinking_text}\n</thinking>\n\n{answer_text}"
            if thinking_text else answer_text
        )

        msg_id = "chatcmpl-cot"
        _first_frame = {
            "id": msg_id,
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        if compliance_disclosure and accept_compliance_events:
            from app.compliance import sse_prelude_openai_inject
            sse_prelude_openai_inject(_first_frame, compliance_disclosure)
        yield f"data: {json.dumps(_first_frame)}\n\n".encode()
        chunk_size = 50
        for i in range(0, len(full_text), chunk_size):
            piece = json.dumps(full_text[i:i + chunk_size])[1:-1]
            yield (
                f'data: {{"id":"{msg_id}","object":"chat.completion.chunk",'
                f'"choices":[{{"index":0,"delta":{{"content":"{piece}"}},"finish_reason":null}}]}}\n\n'
            ).encode()
        yield (
            f'data: {{"id":"{msg_id}","object":"chat.completion.chunk",'
            f'"choices":[{{"index":0,"delta":{{}},"finish_reason":"stop"}}]}}\n\n'
        ).encode()
        yield b"data: [DONE]\n\n"

        await record_outcome(db, provider_id, model, endpoint="completions", success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id)

    except Exception as e:
        await record_outcome(db, provider_id, model, endpoint="completions", success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"error": str(e)}).encode() + b'\n\n')
        yield b'data: [DONE]\n\n'


async def _stream_openai(
    model: str, messages: list, extra: dict, provider_id: str,
    db: AsyncSession, key_record_id: str, t0: float, budget_total: int = 0,
    cache_decision=None,
    # v5.0.0 — compliance substitution disclosure (decision 15 + 28).
    # When both are set, inject ``compliance_substitution`` as a top-level
    # key on the FIRST ``data:`` frame (opt-in via Accept-Compliance-Events).
    compliance_disclosure: dict | None = None,
    accept_compliance_events: bool = False,
) -> AsyncIterator[bytes]:
    in_tok = out_tok = 0
    ttft_ms: float = 0.0
    first_chunk = True
    full_text_buf: list[str] = []
    # v5.0.0 — gate set when the first emitted (post-buffer) frame needs
    # the disclosure key injected. Cleared after first injection.
    _need_compliance_inject = bool(compliance_disclosure and accept_compliance_events)
    # v4.4.6 BUG-057 — buffer one chunk so we can patch the FINAL
    # chunk before emit. Modern OpenAI streaming (when usage is
    # included, which litellm defaults to ON in 1.83.x) emits an
    # extra "usage chunk" AFTER the finish_reason chunk: chunk N-1
    # has finish_reason='stop' / content=None, chunk N has
    # usage={...} / finish_reason=null. The proxy used to pass
    # through verbatim, so the LAST emitted chunk had no
    # finish_reason — breaking OpenAI SDK clients that read the
    # last chunk to detect end-of-stream. Buffer-and-patch: track
    # the most recent finish_reason seen; on end-of-stream, if the
    # last chunk lacks finish_reason and we saw one earlier, copy
    # it onto the last chunk before serializing. Preserves usage
    # info AND end-of-stream signal.
    prev_chunk = None
    last_finish: str | None = None
    try:
        response = await acompletion_with_retry(model=model, messages=messages, stream=True, **extra)
        async for chunk in response:
            if first_chunk:
                ttft_ms = (time.monotonic() - t0) * 1000
                first_chunk = False
            if hasattr(chunk, "usage") and chunk.usage:
                in_tok = getattr(chunk.usage, "prompt_tokens", in_tok)
                out_tok = getattr(chunk.usage, "completion_tokens", out_tok)
            try:
                choice0 = chunk.choices[0] if chunk.choices else None
                if choice0 and getattr(choice0, "finish_reason", None):
                    last_finish = choice0.finish_reason
                delta = getattr(choice0, "delta", None) if choice0 else None
                text = getattr(delta, "content", None) if delta else None
                if text:
                    full_text_buf.append(text)
            except Exception:
                pass
            # Emit the previously-buffered chunk; hold the current
            # one in case it's the last.
            if prev_chunk is not None:
                _payload = prev_chunk.model_dump_json()
                if _need_compliance_inject:
                    from app.compliance import sse_prelude_openai_inject
                    try:
                        _dict = json.loads(_payload)
                        sse_prelude_openai_inject(_dict, compliance_disclosure)
                        _payload = json.dumps(_dict)
                    except Exception:
                        pass  # never break the stream over disclosure
                    _need_compliance_inject = False
                yield f"data: {_payload}\n\n".encode()
            prev_chunk = chunk
        # End-of-stream: patch the final chunk if needed, then emit.
        if prev_chunk is not None:
            try:
                if prev_chunk.choices and last_finish:
                    c0 = prev_chunk.choices[0]
                    if not getattr(c0, "finish_reason", None):
                        c0.finish_reason = last_finish
            except Exception:
                pass  # best-effort; never break the stream
            _payload = prev_chunk.model_dump_json()
            if _need_compliance_inject:
                # Edge case: only one chunk total. Inject on this last
                # emission instead.
                from app.compliance import sse_prelude_openai_inject
                try:
                    _dict = json.loads(_payload)
                    sse_prelude_openai_inject(_dict, compliance_disclosure)
                    _payload = json.dumps(_dict)
                except Exception:
                    pass
                _need_compliance_inject = False
            yield f"data: {_payload}\n\n".encode()
        # v5.0.12 — removed mid-stream ``event: budget`` SSE frame. Vercel
        # AI SDK consumers (OpenCode, Cursor IDE, continue.dev) Zod-validate
        # every ``data:`` line against {choices}|{error}; the budget frame
        # crashed them on invalid_union. Budget info still on the
        # ``X-Token-Budget-Remaining`` response header (set pre-stream).
        yield b"data: [DONE]\n\n"
        await record_outcome(db, provider_id, model, endpoint="completions", success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0,
                             key_record_id=key_record_id, ttft_ms=ttft_ms)
        if cache_decision is not None and cache_decision.eligible:
            try:
                await maybe_store(cache_decision, "".join(full_text_buf))
            except Exception:
                pass
    except Exception as e:
        await record_outcome(db, provider_id, model, endpoint="completions", success=False,
                             key_record_id=key_record_id, error_str=str(e))
        yield (b'data: ' + json.dumps({"error": str(e)}).encode() + b'\n\n')
        yield b'data: [DONE]\n\n'


async def _webhook_completion_openai(
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
        await record_outcome(db, provider_id, model, endpoint="completions", success=True,
                             in_tok=in_tok, out_tok=out_tok, t0=t0, key_record_id=key_record_id)
        await post_webhook(webhook_url, {
            "provider": provider_id,
            "model": model,
            "response": result.model_dump(),
        })
    except Exception as exc:
        await record_outcome(db, provider_id, model, endpoint="completions", success=False,
                             key_record_id=key_record_id, error_str=str(exc))
        await post_webhook(webhook_url, {"error": str(exc), "model": model})
