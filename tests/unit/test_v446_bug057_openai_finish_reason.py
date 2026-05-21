"""v4.4.6 BUG-057 — OpenAI streaming `finish_reason` on the last chunk.

Surfaced 2026-05-20 by `test_compatibility_matrix.py::test_openai_stream_all_providers`
during the L1 `--run-real` matrix run.

Root cause (`app/api/_completions_streaming.py::_stream_openai`): modern OpenAI
streaming (with usage stats included, which litellm defaults to ON in 1.83.x)
emits TWO chunks at end-of-stream:

  chunk N-1: { finish_reason: "stop", delta: { content: null }, ...  }
  chunk N  : { finish_reason: null,   delta: { content: null }, usage: {...} }

The proxy used to pass through verbatim, so the LAST emitted chunk had no
`finish_reason`. OpenAI SDK clients that read the last chunk to detect
end-of-stream would block or misreport.

Fix: buffer one chunk so the FINAL chunk can be patched. Track the most recent
`finish_reason` seen; on end-of-stream, if the last chunk lacks one and we saw
one earlier, copy it onto the last chunk before serializing. Preserves the
usage info AND restores the end-of-stream signal.

Live capture confirming the shape (Devin Personal OpenAI ChatGPT, gpt-4o,
"Say OK", max_tokens=20):

  chunk #1: finish_reason=None,  delta.content=''
  chunk #2: finish_reason=None,  delta.content='OK'
  chunk #3: finish_reason='stop', delta.content=None
  chunk #4: finish_reason=None,  delta.content=None, usage={completion_tokens: 1, ...}

  Total chunks: 4
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


# ── Source-level guards ──────────────────────────────────────────


def test_bug057_fix_marker_present():
    """BUG-057 inline comment is in _stream_openai."""
    src = Path("app/api/_completions_streaming.py").read_text()
    idx = src.index("async def _stream_openai(")
    fn = src[idx:idx + 6000]
    assert "BUG-057" in fn


def test_bug057_buffer_strategy_in_source():
    """Source-level: the fix uses a prev_chunk buffer + last_finish
    tracker, and patches the final chunk before emit."""
    src = Path("app/api/_completions_streaming.py").read_text()
    idx = src.index("async def _stream_openai(")
    fn = src[idx:idx + 6000]
    assert "prev_chunk" in fn
    assert "last_finish" in fn
    # The patch happens conditionally — only when last chunk lacks
    # finish_reason AND we saw one earlier in the stream.
    assert "not getattr(c0, \"finish_reason\", None)" in fn
    assert "c0.finish_reason = last_finish" in fn


def test_bug057_existing_first_chunk_ttft_path_preserved():
    """The first_chunk / ttft_ms timing path must be preserved.
    The buffer-and-patch fix changes WHEN chunks emit, not WHAT they
    contain, except for the final-chunk finish_reason injection."""
    src = Path("app/api/_completions_streaming.py").read_text()
    idx = src.index("async def _stream_openai(")
    fn = src[idx:idx + 6000]
    assert "if first_chunk:" in fn
    assert "ttft_ms = (time.monotonic() - t0) * 1000" in fn


# ── Behavioral tests with mock litellm streams ───────────────────


class _MockChoice:
    """Mutable chat-completion-chunk choice. The fix mutates
    finish_reason on the last chunk's choice in place, so the mock
    needs to allow attribute writes."""
    def __init__(self, finish_reason=None, content=None):
        self.finish_reason = finish_reason
        self.delta = SimpleNamespace(content=content, role=None, tool_calls=None)
        self.index = 0


class _MockChunk:
    def __init__(self, finish_reason=None, content=None, usage=None):
        self.choices = [_MockChoice(finish_reason=finish_reason, content=content)]
        self.usage = usage

    def model_dump_json(self):
        import json
        usage = self.usage
        if usage is not None and hasattr(usage, "__dict__"):
            # SimpleNamespace → dict for JSON serializability
            usage = vars(usage)
        return json.dumps({
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "finish_reason": self.choices[0].finish_reason,
                "delta": {"content": self.choices[0].delta.content},
            }],
            "usage": usage,
        })


async def _mock_openai_stream_with_usage_chunk(*args, **kwargs):
    """Simulates the modern OpenAI streaming pattern: a finish_reason
    chunk followed by a separate usage chunk (litellm default since
    1.83.x with stream_options.include_usage=true)."""
    async def gen():
        yield _MockChunk(finish_reason=None, content="")           # role chunk
        yield _MockChunk(finish_reason=None, content="OK")          # content
        yield _MockChunk(finish_reason="stop", content=None)        # finish
        yield _MockChunk(finish_reason=None, content=None,          # usage
                         usage=SimpleNamespace(prompt_tokens=9, completion_tokens=1))
    return gen()


async def _mock_openai_stream_classic(*args, **kwargs):
    """Simulates the old-style OpenAI streaming where the final chunk
    has finish_reason set directly (no separate usage chunk)."""
    async def gen():
        yield _MockChunk(finish_reason=None, content="")
        yield _MockChunk(finish_reason=None, content="OK")
        yield _MockChunk(finish_reason="stop", content=None)
    return gen()


def _collect_chunks(byte_chunks):
    import json
    full = b"".join(byte_chunks).decode("utf-8", errors="ignore")
    chunks = []
    for line in full.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        body = line[6:]
        if body == "[DONE]":
            continue
        try:
            chunks.append(json.loads(body))
        except ValueError:
            pass
    return chunks


@pytest.mark.asyncio
async def test_bug057_usage_chunk_pattern_patches_finish_reason():
    """End-to-end: modern OpenAI usage-chunk pattern → the proxy
    must emit a final chunk that has finish_reason='stop' AND
    preserves the usage info from the original usage chunk."""
    from app.api import _completions_streaming as cs

    with patch.object(cs, "acompletion_with_retry",
                      _mock_openai_stream_with_usage_chunk), \
         patch.object(cs, "record_outcome", AsyncMock()):
        bytes_out = []
        async for c in cs._stream_openai(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            extra={}, provider_id="prov-test",
            db=None, key_record_id="key-test", t0=0.0,
        ):
            bytes_out.append(c)

    chunks = _collect_chunks(bytes_out)
    assert len(chunks) == 4, f"expected 4 emitted chunks, got {len(chunks)}: {chunks}"
    # All 4 chunks must have object='chat.completion.chunk' (test
    # invariant from test_openai_stream_all_providers).
    for c in chunks:
        assert c.get("object") == "chat.completion.chunk"
    # The LAST chunk must have finish_reason set (the BUG-057 fix).
    last = chunks[-1]
    assert last["choices"][0]["finish_reason"] == "stop", \
        f"BUG-057 regression: last chunk finish_reason should be 'stop', got " \
        f"{last['choices'][0]['finish_reason']!r}"
    # The usage info must be preserved on the last chunk (we didn't
    # drop or move it).
    assert last.get("usage") is not None, \
        "usage info from the original usage chunk must be preserved"
    assert last["usage"].get("completion_tokens") == 1


@pytest.mark.asyncio
async def test_bug057_classic_stream_unaffected():
    """Regression guard: streams that already have finish_reason on
    the last chunk (old-style) must not be modified by the fix."""
    from app.api import _completions_streaming as cs

    with patch.object(cs, "acompletion_with_retry",
                      _mock_openai_stream_classic), \
         patch.object(cs, "record_outcome", AsyncMock()):
        bytes_out = []
        async for c in cs._stream_openai(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            extra={}, provider_id="prov-test",
            db=None, key_record_id="key-test", t0=0.0,
        ):
            bytes_out.append(c)

    chunks = _collect_chunks(bytes_out)
    assert len(chunks) == 3, f"expected 3 emitted chunks, got {len(chunks)}"
    # The last chunk already had finish_reason='stop' from upstream;
    # the fix should not duplicate or modify it.
    last = chunks[-1]
    assert last["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_bug057_no_finish_reason_anywhere_emits_as_is():
    """Defensive: if upstream produces a stream with NO finish_reason
    on any chunk at all (pathological case), the fix must not invent
    one — leaves the last chunk's finish_reason as null. (The fix's
    `if last_finish:` guard prevents this.)"""
    from app.api import _completions_streaming as cs

    async def _no_finish(*args, **kwargs):
        async def gen():
            yield _MockChunk(finish_reason=None, content="")
            yield _MockChunk(finish_reason=None, content="hi")
        return gen()

    with patch.object(cs, "acompletion_with_retry", _no_finish), \
         patch.object(cs, "record_outcome", AsyncMock()):
        bytes_out = []
        async for c in cs._stream_openai(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            extra={}, provider_id="prov-test",
            db=None, key_record_id="key-test", t0=0.0,
        ):
            bytes_out.append(c)

    chunks = _collect_chunks(bytes_out)
    assert len(chunks) == 2
    # No finish_reason was seen anywhere, so we don't invent one.
    # (If upstream is broken, we faithfully report broken.)
    last = chunks[-1]
    assert last["choices"][0]["finish_reason"] is None
