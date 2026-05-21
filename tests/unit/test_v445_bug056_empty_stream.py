"""v4.4.5 BUG-056 — Gemini empty-stream `content_block_start`/`_stop` regression.

Surfaced 2026-05-20 by `test_compatibility_matrix.py::test_anthropic_stream_all_providers`
during the L1 `--run-real` matrix run.

Root cause (`app/api/_messages_streaming.py::_stream_anthropic`): for upstream
providers where the entire stream emits no `delta.content` (Gemini sometimes
buffers the whole response into a single chunk where `delta.content=None` and
only `finish_reason` is set, especially when truncated at `max_tokens`), the
proxy never flips `text_started=True` and therefore never emits any
`content_block_start` / `content_block_stop` events. The resulting SSE stream
has `message_start` → `message_delta` → `message_stop` with no content block
events — which is structurally invalid Anthropic streaming protocol.

Anthropic SDK clients (`anthropic-python`, `anthropic-sdk-typescript`) rely on
`content_block_start` to construct the assistant message object; without it
they return empty/null content even when the server's `message_delta` body
indicates the model ran.

Fix: always emit at least one `content_block_start` + `content_block_stop`
pair. If real text or tool content was streamed, the existing in-loop logic
emits `_start` already, and the end-of-loop emits `_stop`. If neither fired,
emit a synthetic empty text block.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


# ── Source-level guards (cheap, fast) ─────────────────────────────


def test_bug056_empty_stream_fix_present_in_source():
    """The fix block is wired into _stream_anthropic at the end of
    the main streaming loop."""
    src = Path("app/api/_messages_streaming.py").read_text()
    idx = src.index("async def _stream_anthropic(")
    fn = src[idx:idx + 15000]
    # New BUG-056 block emits content_block_start for the empty case
    assert "BUG-056" in fn
    # The conditional checks both text_started and tool_started are False
    assert "if not text_started and not tool_started:" in fn
    # And both content_block_start and content_block_stop are emitted
    # when neither flag is set (synthetic empty text block)
    assert '"type":"content_block_start"' in fn
    assert '"type":"content_block_stop"' in fn


def test_bug056_existing_content_path_still_emits_stop():
    """The else-branch (text_started or tool_started=True) must still
    emit content_block_stop — that's the pre-fix behavior we want to
    preserve, just wrapped differently."""
    src = Path("app/api/_messages_streaming.py").read_text()
    idx = src.index("async def _stream_anthropic(")
    fn = src[idx:idx + 15000]
    # After the BUG-056 block, the else branch must contain
    # content_block_stop emission (the non-empty case)
    bug056_idx = fn.index("BUG-056")
    block = fn[bug056_idx:bug056_idx + 1500]
    assert "else:" in block
    # The else branch emits ONLY content_block_stop (start was already
    # emitted in-loop on first content)
    after_else = block[block.index("else:"):block.index("if output_tokens")]
    assert '"type":"content_block_stop"' in after_else


# ── Behavioral test with mock litellm stream ──────────────────────


class _MockChunk:
    """Approximate the litellm ModelResponseStream shape."""
    def __init__(self, content=None, finish_reason=None):
        delta = SimpleNamespace(
            content=content, tool_calls=None, role=None,
        )
        choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
        self.choices = [choice]
        self.usage = None


async def _mock_acompletion_empty_stream(*args, **kwargs):
    """Returns an async iterator with a single chunk that has
    delta.content=None + finish_reason='stop' — the Gemini empty-
    response pattern."""
    async def gen():
        yield _MockChunk(content=None, finish_reason="stop")
    return gen()


async def _mock_acompletion_text_stream(*args, **kwargs):
    """Returns an async iterator with content + final-chunk pattern."""
    async def gen():
        yield _MockChunk(content="Hello", finish_reason=None)
        yield _MockChunk(content=None, finish_reason="stop")
    return gen()


def _collect_events_from_sse(byte_chunks: list[bytes]) -> list[dict]:
    """Parse SSE-formatted chunks into a list of event dicts."""
    import json
    events = []
    full = b"".join(byte_chunks).decode("utf-8", errors="ignore")
    for line in full.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        body = line[6:]
        if body == "[DONE]":
            continue
        try:
            events.append(json.loads(body))
        except ValueError:
            pass
    return events


@pytest.mark.asyncio
async def test_bug056_empty_stream_emits_synthetic_content_block():
    """End-to-end: when litellm emits a single chunk with no content,
    the proxy must still produce a structurally valid Anthropic
    stream including content_block_start + content_block_stop."""
    from app.api import _messages_streaming as ms

    with patch.object(ms, "acompletion_with_retry", _mock_acompletion_empty_stream), \
         patch.object(ms, "record_outcome", AsyncMock()):
        chunks = []
        async for ch in ms._stream_anthropic(
            model="gemini/gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            extra={}, provider_id="prov-test",
            db=None, key_record_id="key-test", t0=0.0,
        ):
            chunks.append(ch)

    events = _collect_events_from_sse(chunks)
    types = [e.get("type") for e in events]

    # Required set per Anthropic streaming protocol — every message
    # must have at least one content block framed by start/_stop.
    for required in ("message_start", "content_block_start",
                     "content_block_stop", "message_delta", "message_stop"):
        assert required in types, \
            f"BUG-056 regression: missing {required!r} in empty-stream " \
            f"output. Got: {types}"

    # The synthetic content block must be a text block (not tool_use)
    # — empty providers default to empty text.
    starts = [e for e in events if e.get("type") == "content_block_start"]
    assert starts, "expected at least one content_block_start"
    assert starts[0]["content_block"]["type"] == "text", \
        "empty-response synthetic block must be a text block"
    assert starts[0]["content_block"]["text"] == "", \
        "synthetic text block must have empty text"


@pytest.mark.asyncio
async def test_bug056_text_stream_still_emits_normally():
    """Regression guard: the normal-content path must still emit
    content_block_start (in-loop) and content_block_stop (post-loop),
    NOT a synthetic empty block."""
    from app.api import _messages_streaming as ms

    with patch.object(ms, "acompletion_with_retry", _mock_acompletion_text_stream), \
         patch.object(ms, "record_outcome", AsyncMock()):
        chunks = []
        async for ch in ms._stream_anthropic(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            extra={}, provider_id="prov-test",
            db=None, key_record_id="key-test", t0=0.0,
        ):
            chunks.append(ch)

    events = _collect_events_from_sse(chunks)
    types = [e.get("type") for e in events]

    # All 5 required event types present
    for required in ("message_start", "content_block_start",
                     "content_block_delta", "content_block_stop",
                     "message_delta", "message_stop"):
        assert required in types, f"missing {required!r}. Got: {types}"

    # Exactly ONE content_block_start (the real one, in-loop) — NOT
    # the synthetic + real (would mean fix double-emits)
    starts = [e for e in events if e.get("type") == "content_block_start"]
    assert len(starts) == 1, \
        f"expected exactly 1 content_block_start (in-loop emission), " \
        f"got {len(starts)}. Fix must not double-emit."
    # And the content_block_start must NOT be the synthetic empty case
    # — the in-loop emission has text='' but the delta that follows
    # carries the actual text. So the start itself is text='' either
    # way; we verify the delta has real content.
    deltas = [e for e in events if e.get("type") == "content_block_delta"]
    assert deltas, "non-empty stream must have content_block_delta"
    assert deltas[0]["delta"]["text"] == "Hello"


@pytest.mark.asyncio
async def test_bug056_empty_stream_does_not_break_other_envelope():
    """The fix must not affect the surrounding message envelope —
    message_start at the top, message_delta + message_stop at the
    bottom — only the content_block layer is changed."""
    from app.api import _messages_streaming as ms

    with patch.object(ms, "acompletion_with_retry", _mock_acompletion_empty_stream), \
         patch.object(ms, "record_outcome", AsyncMock()):
        chunks = []
        async for ch in ms._stream_anthropic(
            model="gemini/gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            extra={}, provider_id="prov-test",
            db=None, key_record_id="key-test", t0=0.0,
        ):
            chunks.append(ch)

    events = _collect_events_from_sse(chunks)
    types = [e.get("type") for e in events]

    # The exact ordering Anthropic SDK clients expect:
    # message_start → content_block_start → content_block_stop →
    # message_delta → message_stop
    # (No content_block_delta in the empty case — that's OK; SDK
    # treats absence-of-delta as empty content.)
    assert types == [
        "message_start", "content_block_start", "content_block_stop",
        "message_delta", "message_stop",
    ], f"unexpected event order. Got: {types}"
