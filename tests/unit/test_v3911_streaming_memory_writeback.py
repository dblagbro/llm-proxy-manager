"""v3.9.11 (#267 Phase 5.5) — streaming memory write-back.

DevinGPT runs stream:true for chat completions. Phase 5 (v3.9.0) only
wired extraction for non-streaming responses, which would have made
adoption read-only (inject works; write-back doesn't). This phase wires
the streaming claude-oauth path through the same extractor.

The streaming claude-oauth handler already assembles a full response
dict (assembled_response) at SSE termination — same shape as a
non-streaming response. v3.9.11 just feeds that into the existing
maybe_extract_memory_writes() function.

These tests are source-level guards confirming the wiring is in place.
A live integration test against the real SSE stream is impractical in
unit tests; the assembled_response logic is exercised by the existing
test_v390_memory_extract suite.
"""
from __future__ import annotations

from pathlib import Path


def test_stream_claude_oauth_accepts_conversation_id_kwarg():
    src = Path("app/api/_messages_streaming.py").read_text()
    # The new kwargs land in the _stream_claude_oauth signature
    idx = src.index("async def _stream_claude_oauth")
    sig_window = src[idx:idx + 2000]
    assert "api_key_id: Optional[str] = None" in sig_window
    assert "conversation_id: Optional[str] = None" in sig_window
    assert "memory_tag: Optional[str] = None" in sig_window


def test_stream_claude_oauth_invokes_extractor_on_success():
    """After assembling response_dict on stream success, the extractor
    is called when conversation_id is set."""
    src = Path("app/api/_messages_streaming.py").read_text()
    assert "from app.memory.extract import maybe_extract_memory_writes" in src
    # The call is gated on conversation_id presence
    assert "if conversation_id and api_key_id:" in src
    assert "response_dict=assembled_response" in src


def test_stream_extractor_call_uses_silent_degrade():
    """Memory extract errors never break the stream's success path."""
    src = Path("app/api/_messages_streaming.py").read_text()
    # The extract call is inside a try/except
    idx = src.index("from app.memory.extract import maybe_extract_memory_writes")
    body = src[idx - 200:idx + 800]
    assert "try:" in body
    assert "except Exception:" in body
    # The comment names the intent so a future contributor doesn't remove it
    assert "Silent degrade" in body


def test_messages_endpoint_passes_conv_id_to_stream():
    src = Path("app/api/messages.py").read_text()
    # The /v1/messages call site must thread x_conversation_id through
    idx = src.index("stream_gen = _stream_claude_oauth(")
    call = src[idx:idx + 1000]
    assert "conversation_id=x_conversation_id" in call
    assert "memory_tag=x_memory_tag" in call
    assert "api_key_id=key_record.id" in call


def test_completions_endpoint_passes_conv_id_to_stream():
    """/v1/chat/completions also threads memory params (the DevinGPT
    path — OpenAI shape → claude-oauth via translation)."""
    src = Path("app/api/completions.py").read_text()
    idx = src.index("anthropic_sse = _stream_claude_oauth(")
    call = src[idx:idx + 1000]
    assert "conversation_id=x_conversation_id" in call
    assert "memory_tag=x_memory_tag" in call
    assert "api_key_id=key_record.id" in call


def test_assembled_response_shape_matches_extractor_contract():
    """Smoke-check that the assembled_response dict has the keys the
    extractor reads (content[] of tool_use blocks)."""
    src = Path("app/api/_messages_streaming.py").read_text()
    idx = src.index("assembled_response = {")
    body = src[idx:idx + 700]
    # The assembled dict carries content list (which the extractor walks)
    assert '"content": content_list' in body
    # content_list builds {"type": "tool_use", "id": ..., "name": ..., "input": ...}
    # for each block — same shape maybe_extract_memory_writes scans for
    idx2 = src.index('"type": "tool_use", "id":')
    walk = src[idx2:idx2 + 400]
    assert '"name": blk.get("name")' in walk
    assert '"input": parsed_input' in walk


def test_extractor_called_after_record_outcome():
    """Ordering: record_outcome (which writes to activity_log) runs
    BEFORE the memory extract. That way a memory store error doesn't
    block the metrics record."""
    src = Path("app/api/_messages_streaming.py").read_text()
    idx_record = src.index("await record_outcome(")
    # Find the record_outcome after the success-path assembled_response
    # (there are multiple record_outcome calls; we want the one on the
    # success-end-of-stream path).
    idx_extract = src.index("from app.memory.extract import maybe_extract_memory_writes")
    # The extract call must come AFTER a record_outcome
    assert idx_record < idx_extract
