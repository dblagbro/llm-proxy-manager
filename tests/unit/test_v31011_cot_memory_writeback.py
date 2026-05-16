"""v3.10.11 — caller-memory write-back for the CoT streaming path.

`_stream_cot_anthropic` was the one streaming path that never called
`maybe_extract_memory_writes`. These tests verify it now (a) accumulates
memory-tool `tool_use` blocks from the SSE passthrough and (b) feeds the
assembled response through the extractor when `conversation_id` is set —
and stays a no-op when the caller did not opt into memory.
"""
from __future__ import annotations

import json

import pytest


def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


@pytest.mark.asyncio
async def test_cot_stream_runs_memory_extract(monkeypatch):
    import app.api._messages_streaming as st
    import app.memory.extract as mem

    async def fake_cot_pipeline(*a, **k):
        yield _sse({"type": "content_block_start", "index": 0,
                    "content_block": {"type": "tool_use", "id": "toolu_1",
                                      "name": "memory_20250818", "input": {}}})
        yield _sse({"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta",
                              "partial_json": '{"command": '}})
        yield _sse({"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta",
                              "partial_json": '"view"}'}})
        yield _sse({"type": "content_block_stop", "index": 0})
        yield _sse({"type": "message_delta",
                    "usage": {"input_tokens": 5, "output_tokens": 7}})

    async def fake_record_outcome(*a, **k):
        return None

    captured = {}

    async def fake_extract(db, *, response_dict, api_key_id, conversation_id,
                           memory_tag_default=None, source_provider_id=None):
        captured["response_dict"] = response_dict
        captured["conversation_id"] = conversation_id
        captured["api_key_id"] = api_key_id
        captured["source_provider_id"] = source_provider_id
        return 1

    monkeypatch.setattr(st, "run_cot_pipeline", fake_cot_pipeline)
    monkeypatch.setattr(st, "record_outcome", fake_record_outcome)
    monkeypatch.setattr(mem, "maybe_extract_memory_writes", fake_extract)

    async for _ in st._stream_cot_anthropic(
        "claude-x", [], None, {}, None, "prov-1", object(), "key-1",
        conversation_id="chat-abc", memory_tag="memory_20250818",
    ):
        pass

    assert captured, "maybe_extract_memory_writes was not called"
    assert captured["conversation_id"] == "chat-abc"
    assert captured["api_key_id"] == "key-1"
    assert captured["source_provider_id"] == "prov-1"
    blocks = captured["response_dict"]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["name"] == "memory_20250818"
    # the split input_json_delta chunks must reassemble into valid JSON
    assert blocks[0]["input"] == {"command": "view"}


@pytest.mark.asyncio
async def test_cot_stream_no_conversation_id_skips_extract(monkeypatch):
    """Without `conversation_id` the caller did not opt into memory —
    extract must not run."""
    import app.api._messages_streaming as st
    import app.memory.extract as mem

    async def fake_cot_pipeline(*a, **k):
        yield _sse({"type": "message_delta",
                    "usage": {"input_tokens": 1, "output_tokens": 1}})

    async def fake_record_outcome(*a, **k):
        return None

    called = []

    async def fake_extract(*a, **k):
        called.append(1)
        return 0

    monkeypatch.setattr(st, "run_cot_pipeline", fake_cot_pipeline)
    monkeypatch.setattr(st, "record_outcome", fake_record_outcome)
    monkeypatch.setattr(mem, "maybe_extract_memory_writes", fake_extract)

    async for _ in st._stream_cot_anthropic(
        "claude-x", [], None, {}, None, "prov-1", object(), "key-1",
    ):
        pass

    assert not called, "extract should not run without conversation_id"


@pytest.mark.asyncio
async def test_cot_stream_extract_runs_even_with_no_tool_blocks(monkeypatch):
    """A memory-enabled CoT request with no memory-tool blocks still
    calls extract (it records an `extract/skipped` metric) — matching
    the other streaming paths."""
    import app.api._messages_streaming as st
    import app.memory.extract as mem

    async def fake_cot_pipeline(*a, **k):
        yield _sse({"type": "message_delta",
                    "usage": {"input_tokens": 1, "output_tokens": 1}})

    async def fake_record_outcome(*a, **k):
        return None

    captured = {}

    async def fake_extract(db, *, response_dict, **k):
        captured["response_dict"] = response_dict
        return 0

    monkeypatch.setattr(st, "run_cot_pipeline", fake_cot_pipeline)
    monkeypatch.setattr(st, "record_outcome", fake_record_outcome)
    monkeypatch.setattr(mem, "maybe_extract_memory_writes", fake_extract)

    async for _ in st._stream_cot_anthropic(
        "claude-x", [], None, {}, None, "prov-1", object(), "key-1",
        conversation_id="chat-empty",
    ):
        pass

    assert captured, "extract should run for a memory-enabled request"
    assert captured["response_dict"]["content"] == []
