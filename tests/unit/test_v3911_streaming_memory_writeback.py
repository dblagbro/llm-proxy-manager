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

# v4.4.12 split: claude-oauth dispatch moved to _messages_streaming_oauth.py.
# Source-level tests that used to read just _messages_streaming.py now read
# both files (concatenated) so they continue to check the same surface area.
from tests.unit._handler_surface import completions_handler_source


def _read_streaming_src() -> str:
    return (
        Path("app/api/_messages_streaming.py").read_text()
        + "\n\n# ── _messages_streaming_oauth.py ──\n\n"
        + Path("app/api/_messages_streaming_oauth.py").read_text()
    )


def test_stream_claude_oauth_accepts_conversation_id_kwarg():
    from inspect import signature
    from typing import get_type_hints

    from app.api._messages_streaming_oauth import _stream_claude_oauth

    params = signature(_stream_claude_oauth).parameters
    hints = get_type_hints(_stream_claude_oauth)
    for name in ("api_key_id", "conversation_id", "memory_tag"):
        assert params[name].default is None
        assert hints[name] == str | None


def test_stream_claude_oauth_invokes_extractor_on_success():
    """After assembling response_dict on stream success, the extractor
    is called when conversation_id is set."""
    src = _read_streaming_src()
    assert "from app.memory.extract import maybe_extract_memory_writes" in src
    # The call is gated on conversation_id presence
    assert "if conversation_id and api_key_id:" in src
    assert "response_dict=assembled_response" in src


def test_stream_extractor_call_uses_silent_degrade():
    """Memory extract errors never break the stream's success path.

    v3.9.14 added a second extract site in _stream_anthropic; this test
    walks each occurrence and confirms ALL are wrapped in try/except
    with the 'Silent degrade' comment locked in. v4.4.12 split adds a
    third occurrence in the new _messages_streaming_oauth.py module.
    """
    src = _read_streaming_src()
    needle = "from app.memory.extract import maybe_extract_memory_writes"
    start = 0
    occurrences = 0
    while True:
        idx = src.find(needle, start)
        if idx < 0:
            break
        occurrences += 1
        body = src[idx - 1800 : idx + 1000]
        assert "try:" in body, f"extract call #{occurrences} missing try wrapper"
        assert "except Exception:" in body, f"extract call #{occurrences} missing except"
        assert "Silent degrade" in body, (
            f"extract call #{occurrences} missing 'Silent degrade' comment"
        )
        start = idx + len(needle)
    assert occurrences >= 1, "no extract calls found in streaming source"


def test_messages_endpoint_passes_conv_id_to_stream():
    # v3.10.9 — the claude-oauth streaming dispatch moved out of
    # messages.py into _messages_dispatch.py; the conv/tag/key wiring
    # moved with it (params are named conversation_id / memory_tag there).
    src = Path("app/api/_messages_dispatch.py").read_text()
    idx = src.index("stream_gen = _stream_claude_oauth(")
    call = src[idx : idx + 1000]
    assert "conversation_id=conversation_id" in call
    assert "memory_tag=memory_tag" in call
    assert "api_key_id=key_record.id" in call


def test_completions_endpoint_passes_conv_id_to_stream():
    """/v1/chat/completions also threads memory params (the DevinGPT
    path — OpenAI shape → claude-oauth via translation)."""
    # v5.22.37 — reads the whole handler surface: the own-dispatcher branches
    # this asserts on moved into _completions_{claude_oauth,grok_web}.py when
    # completions.py was split to meet its 900-LOC pin.
    src = completions_handler_source()
    idx = src.index("anthropic_sse = _stream_claude_oauth(")
    call = src[idx : idx + 1000]
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
    import ast

    src = _read_streaming_src()
    idx = src.index("assembled_response = {")
    body = src[idx : idx + 700]
    # The assembled dict carries the content list the extractor walks.
    assert '"content": content_list' in body
    # Inspect every matching tool-use dict independent of line wrapping.
    sites = []
    for path in ("app/api/_messages_streaming.py", "app/api/_messages_streaming_oauth.py"):
        for node in ast.walk(ast.parse(Path(path).read_text())):
            if not isinstance(node, ast.Dict):
                continue
            fields = {
                key.value: value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            type_value = fields.get("type")
            if (
                isinstance(type_value, ast.Constant)
                and type_value.value == "tool_use"
                and "id" in fields
            ):
                sites.append(fields)
    assert sites, "no tool_use assembly site found in streaming source"
    for fields in sites:
        assert "name" in fields, "a tool_use assembly block is missing the name key"
        assert "input" in fields, "a tool_use assembly block is missing the input key"


def test_extractor_called_after_record_outcome():
    """Ordering: record_outcome (which writes to activity_log) runs
    BEFORE the memory extract. That way a memory store error doesn't
    block the metrics record."""
    src = _read_streaming_src()
    idx_record = src.index("await record_outcome(")
    # Find the record_outcome after the success-path assembled_response
    # (there are multiple record_outcome calls; we want the one on the
    # success-end-of-stream path).
    idx_extract = src.index("from app.memory.extract import maybe_extract_memory_writes")
    # The extract call must come AFTER a record_outcome
    assert idx_record < idx_extract
