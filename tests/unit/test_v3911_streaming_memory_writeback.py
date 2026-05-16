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
    """Memory extract errors never break the stream's success path.

    v3.9.14 added a second extract site in _stream_anthropic; this test
    walks each occurrence and confirms ALL are wrapped in try/except
    with the 'Silent degrade' comment locked in.
    """
    src = Path("app/api/_messages_streaming.py").read_text()
    needle = "from app.memory.extract import maybe_extract_memory_writes"
    start = 0
    occurrences = 0
    while True:
        idx = src.find(needle, start)
        if idx < 0:
            break
        occurrences += 1
        body = src[idx - 1800:idx + 1000]
        assert "try:" in body, f"extract call #{occurrences} missing try wrapper"
        assert "except Exception:" in body, f"extract call #{occurrences} missing except"
        assert "Silent degrade" in body, f"extract call #{occurrences} missing 'Silent degrade' comment"
        start = idx + len(needle)
    assert occurrences >= 1, "no extract calls found in _messages_streaming.py"


def test_messages_endpoint_passes_conv_id_to_stream():
    # v3.10.9 — the claude-oauth streaming dispatch moved out of
    # messages.py into _messages_dispatch.py; the conv/tag/key wiring
    # moved with it (params are named conversation_id / memory_tag there).
    src = Path("app/api/_messages_dispatch.py").read_text()
    idx = src.index("stream_gen = _stream_claude_oauth(")
    call = src[idx:idx + 1000]
    assert "conversation_id=conversation_id" in call
    assert "memory_tag=memory_tag" in call
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
    """Smoke-check that the assembled response dicts have the keys the
    extractor reads (content[] of tool_use blocks). There are several
    assembly sites (litellm / claude-oauth / CoT streaming paths); each
    must build a {"type": "tool_use", "id", "name", "input"} block.
    Contract check by key presence — not by exact variable names, which
    differ per site and are an implementation detail."""
    import re
    src = Path("app/api/_messages_streaming.py").read_text()
    idx = src.index("assembled_response = {")
    body = src[idx:idx + 700]
    # The assembled dict carries the content list the extractor walks.
    assert '"content": content_list' in body
    # Every tool_use assembly site must carry the name + input keys.
    sites = [m.start() for m in re.finditer(r'"type": "tool_use", "id":', src)]
    assert sites, "no tool_use assembly site found in _messages_streaming.py"
    for s in sites:
        walk = src[s:s + 400]
        assert '"name":' in walk, "a tool_use assembly block is missing the name key"
        assert '"input":' in walk, "a tool_use assembly block is missing the input key"


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
