"""v3.9.14 — litellm streaming write-back + tighter litellm pin.

Two changes:
1. requirements.txt: litellm pinned ``>=1.83.0,<1.84.0``. The 1.84.0
   release (2026-05-14) ships breaking changes per upstream notes; we
   stay on the 1.83.x stable line until patches settle.
2. ``app/api/_messages_streaming.py::_stream_anthropic`` (the litellm
   path for non-claude-oauth Anthropic-shape providers) now accumulates
   tool_use blocks across SSE chunks + feeds the assembled response
   through the same ``maybe_extract_memory_writes()`` the claude-oauth
   path uses. Closes the last memory write-back gap.

These are source-level guards confirming the wiring is in place + the
pin is tight.
"""
from __future__ import annotations

import inspect
from pathlib import Path


# ── Pin ────────────────────────────────────────────────────────────


def test_litellm_pin_is_tight():
    """Pin must be explicitly bounded — not the old decorative
    ``>=1.40.0`` floor. v3.9.14 set ``<1.84.0``; v3.9.17 widened to
    ``<1.85.0`` after the P4 evaluation found 1.84.0's breaking changes
    are all Proxy-server features (see test_v3917_litellm_pin.py).
    This test just guards that SOME explicit upper bound exists."""
    req = Path("requirements.txt").read_text()
    # Look at the pin line specifically (comments may mention old
    # ceilings for historical context).
    pin_lines = [
        l for l in req.splitlines()
        if l.strip().startswith("litellm") and not l.strip().startswith("#")
    ]
    assert pin_lines, "no litellm pin found"
    pin = pin_lines[0]
    # v4.4.30: bumped past <1.85.0 to address 3 critical CVEs
    # (SSTI in /completions, RCE via eval, OIDC auth bypass).
    assert pin == "litellm>=1.85.2,<1.87.0", (
        f"expected canonical v4.4.30 pin; got {pin!r}"
    )
    # The old loose pin must be gone (otherwise pip resolves to >=1.40)
    assert "litellm>=1.40.0" not in req


# ── Function signature ─────────────────────────────────────────────


def test_stream_anthropic_accepts_memory_kwargs():
    from app.api._messages_streaming import _stream_anthropic
    params = inspect.signature(_stream_anthropic).parameters
    for kw in ("api_key_id", "conversation_id", "memory_tag"):
        assert kw in params, f"missing kwarg {kw}"
        # Optional with None default — preserves backwards-compat with
        # callers that don't pass it
        assert params[kw].default is None


# ── Tool-use accumulator ───────────────────────────────────────────


def test_tool_calls_accumulator_seeded_on_content_block_start():
    """When a tool_use block starts, the accumulator must record
    {id, name, input_str=""} so partial_json deltas can append to it."""
    src = Path("app/api/_messages_streaming.py").read_text()
    # Find the _stream_anthropic function body
    idx = src.index("async def _stream_anthropic(")
    fn = src[idx:idx + 12000]
    assert "tool_calls_acc:" in fn
    assert 'tool_calls_acc.setdefault(tool_id,' in fn
    assert '"input_str": ""' in fn


def test_partial_json_appended_to_accumulator():
    src = Path("app/api/_messages_streaming.py").read_text()
    idx = src.index("async def _stream_anthropic(")
    fn = src[idx:idx + 12000]
    assert 'tool_calls_acc[tool_id]["input_str"] += args_fragment' in fn


def test_assembled_response_built_at_stream_end():
    """End of successful stream → parse accumulated JSON per tool_id,
    build content[] of tool_use blocks, feed through extractor."""
    src = Path("app/api/_messages_streaming.py").read_text()
    idx = src.index("async def _stream_anthropic(")
    fn = src[idx:idx + 12000]
    # Builds content_list of tool_use blocks
    assert 'content_list.append({' in fn
    assert '"type": "tool_use"' in fn
    # Assembled dict has the keys the extractor expects
    assert '"content": content_list' in fn
    assert '"role": "assistant"' in fn


def test_extract_invoked_only_when_conversation_id_set():
    src = Path("app/api/_messages_streaming.py").read_text()
    idx = src.index("async def _stream_anthropic(")
    fn = src[idx:idx + 12000]
    assert "if conversation_id and api_key_id and tool_calls_acc:" in fn


def test_extract_invocation_silent_degrade():
    src = Path("app/api/_messages_streaming.py").read_text()
    idx = src.index("async def _stream_anthropic(")
    fn = src[idx:idx + 12000]
    # The extract block is wrapped in try/except — never breaks stream success
    extract_idx = fn.index("from app.memory.extract import maybe_extract_memory_writes")
    surrounding = fn[extract_idx - 1800:extract_idx + 1000]
    assert "try:" in surrounding
    assert "except Exception:" in surrounding


def test_malformed_json_skipped_not_crashed():
    """If a tool_use block's accumulated input_str fails to parse, we
    skip that block rather than corrupt the extractor's view."""
    src = Path("app/api/_messages_streaming.py").read_text()
    idx = src.index("async def _stream_anthropic(")
    fn = src[idx:idx + 12000]
    # The json.loads is wrapped — malformed JSON → continue, not crash
    assert "except ValueError:" in fn
    assert "# malformed JSON" in fn


# ── Call-site wiring ───────────────────────────────────────────────


def test_messages_endpoint_threads_memory_kwargs_into_stream_anthropic():
    src = Path("app/api/messages.py").read_text()
    # All three _stream_anthropic call sites pass memory kwargs
    occurrences = src.count("conversation_id=x_conversation_id")
    # Two from claude-oauth path (already there pre-v3.9.14), plus
    # three new occurrences from the _stream_anthropic call sites (primary, backup, fallthrough)
    assert occurrences >= 4, (
        f"expected ≥4 conversation_id=x_conversation_id occurrences, got {occurrences}"
    )


def test_no_litellm_version_change_required():
    """v3.9.14 ships memory write-back for the litellm SSE path WITHOUT
    needing a litellm version bump. Our work is entirely in our own
    assembly logic — litellm passes tool_calls through verbatim."""
    src = Path("app/api/_messages_streaming.py").read_text()
    idx = src.index("async def _stream_anthropic(")
    fn = src[idx:idx + 12000]
    # We read tc_delta.function.name and .arguments from litellm's
    # existing streaming shape — no new API surface needed
    assert "getattr(tc_delta," in fn
    assert "getattr(fn," in fn
