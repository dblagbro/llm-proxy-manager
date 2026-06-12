"""v5.3.9 — streaming empty-success guard + provider failover.

Root cause pinned here: a dead upstream (observed live on -s23: both
cursor-oauth providers behind an expired Cursor session) answers HTTP 200
with a syntactically valid SSE stream containing ZERO content deltas —
role chunk / finish chunk / [DONE] and nothing else. ``preflight_sse``
passes it (first frame is not an error frame), so the 200 streamed
end-to-end and every streaming caller (opencode via the hub relay) got a
silent EMPTY completion with rc=0, while the non-streaming path correctly
502'd via ``looks_like_empty_success_failure`` and failed over.

The guard (``buffer_sse_until_content`` + ``stream_with_empty_guard`` in
app/api/_messages_streaming.py) buffers frames until the first meaningful
content delta; a stream that exhausts with none records a circuit-breaker
failure and fails over to the next provider — mirroring the non-streaming
guard + the grok-web failover pattern.
"""
import json

import pytest
from fastapi import HTTPException

from app.api._messages_streaming import (
    _sse_frame_has_content,
    buffer_sse_until_content,
    stream_with_empty_guard,
)


def _frame(payload) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


# ── captured shapes ──────────────────────────────────────────────────────

# OpenAI chunk shapes (what _stream_openai emits)
ROLE_CHUNK = _frame({
    "id": "msg", "object": "chat.completion.chunk",
    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
})
CONTENT_CHUNK = _frame({
    "id": "msg", "object": "chat.completion.chunk",
    "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}],
})
EMPTY_CONTENT_CHUNK = _frame({
    "id": "msg", "object": "chat.completion.chunk",
    "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}],
})
TOOL_CHUNK = _frame({
    "id": "msg", "object": "chat.completion.chunk",
    "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"name": "f"}}]}, "finish_reason": None}],
})
FINISH_CHUNK = _frame({
    "id": "msg", "object": "chat.completion.chunk",
    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
})
DONE_FRAME = b"data: [DONE]\n\n"

# Anthropic event shapes (what _stream_anthropic emits)
MESSAGE_START = _frame({
    "type": "message_start",
    "message": {"id": "msg_proxy", "type": "message", "role": "assistant",
                "content": [], "model": "m", "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}},
})
TEXT_DELTA = _frame({
    "type": "content_block_delta", "index": 0,
    "delta": {"type": "text_delta", "text": "Hi"},
})
EMPTY_TEXT_DELTA = _frame({
    "type": "content_block_delta", "index": 0,
    "delta": {"type": "text_delta", "text": ""},
})
TOOL_BLOCK_START = _frame({
    "type": "content_block_start", "index": 0,
    "content_block": {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {}},
})
TEXT_BLOCK_START = _frame({
    "type": "content_block_start", "index": 0,
    "content_block": {"type": "text", "text": ""},
})
MESSAGE_STOP = _frame({"type": "message_stop"})


# ── _sse_frame_has_content ───────────────────────────────────────────────

@pytest.mark.parametrize("frame,expected", [
    (ROLE_CHUNK, False),
    (CONTENT_CHUNK, True),
    (EMPTY_CONTENT_CHUNK, False),
    (TOOL_CHUNK, True),
    (FINISH_CHUNK, False),
    (DONE_FRAME, False),
    (MESSAGE_START, False),
    (TEXT_DELTA, True),
    (EMPTY_TEXT_DELTA, False),
    (TOOL_BLOCK_START, True),
    (TEXT_BLOCK_START, False),
    (MESSAGE_STOP, False),
    (b"event: compliance_substitution\ndata: {\"type\":\"compliance\"}\n\n", False),
    (b"", False),
])
def test_frame_content_detection(frame, expected):
    assert _sse_frame_has_content(frame) is expected


def test_reasoning_content_counts_as_alive():
    # A provider streaming only reasoning is alive — must NOT be
    # classified as empty-success (avoid failover during long thinking).
    frame = _frame({
        "id": "msg", "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"reasoning_content": "thinking..."}, "finish_reason": None}],
    })
    assert _sse_frame_has_content(frame) is True


# ── buffer_sse_until_content ─────────────────────────────────────────────

async def _gen_from(frames):
    for f in frames:
        yield f


@pytest.mark.asyncio
async def test_dead_cursor_stream_detected_empty():
    # The exact -s23 pattern: role chunk → empty content → finish → [DONE],
    # zero usage. 0 of 400+ requests on the dead providers carried content.
    frames = [ROLE_CHUNK, EMPTY_CONTENT_CHUNK, FINISH_CHUNK, DONE_FRAME]
    gen = _gen_from(frames[1:])
    buffered, has_content, gen = await buffer_sse_until_content(frames[0], gen)
    assert has_content is False
    assert buffered == frames  # every frame preserved for diagnostics


@pytest.mark.asyncio
async def test_content_midstream_passes_with_frames_preserved():
    frames = [ROLE_CHUNK, EMPTY_CONTENT_CHUNK, CONTENT_CHUNK, DONE_FRAME]
    gen = _gen_from(frames[1:])
    buffered, has_content, gen = await buffer_sse_until_content(frames[0], gen)
    assert has_content is True
    assert buffered == frames[:3]  # stops at first content frame
    assert [f async for f in gen] == [DONE_FRAME]  # rest still streamable


@pytest.mark.asyncio
async def test_anthropic_empty_stream_detected():
    frames = [MESSAGE_START, TEXT_BLOCK_START, EMPTY_TEXT_DELTA, MESSAGE_STOP]
    buffered, has_content, _ = await buffer_sse_until_content(
        frames[0], _gen_from(frames[1:]))
    assert has_content is False


@pytest.mark.asyncio
async def test_anthropic_tool_use_counts_as_content():
    frames = [MESSAGE_START, TOOL_BLOCK_START]
    _, has_content, _ = await buffer_sse_until_content(
        frames[0], _gen_from(frames[1:]))
    assert has_content is True


@pytest.mark.asyncio
async def test_frame_cap_passes_through_alive_stream():
    # A slow-but-alive stream past the cap must NEVER be classified empty.
    many = [ROLE_CHUNK] * 100
    buffered, has_content, _ = await buffer_sse_until_content(
        many[0], _gen_from(many[1:]), max_frames=64)
    assert has_content is True
    assert len(buffered) == 64


# ── stream_with_empty_guard failover ─────────────────────────────────────

class _FakeProvider:
    def __init__(self, pid, name, ptype="openai"):
        self.id = pid
        self.name = name
        self.provider_type = ptype


class _FakeRoute:
    def __init__(self, pid, name, model="openai/claude-4-sonnet"):
        self.provider = _FakeProvider(pid, name)
        self.litellm_model = model
        self.litellm_kwargs = {}
        self.native_thinking_params = None


EMPTY_STREAM = [ROLE_CHUNK, EMPTY_CONTENT_CHUNK, FINISH_CHUNK, DONE_FRAME]
GOOD_STREAM = [ROLE_CHUNK, CONTENT_CHUNK, FINISH_CHUNK, DONE_FRAME]


@pytest.mark.asyncio
async def test_failover_from_empty_provider(monkeypatch):
    dead = _FakeRoute("dead-1", "Cursor onec1.com C1 account")
    alive = _FakeRoute("alive-1", "C1 Vertex AI", model="gemini/gemini-2.5-flash")
    failures = []

    async def fake_record_failure(pid, billing_error=False):
        failures.append(pid)

    async def fake_select_provider(db, hint, **kw):
        assert kw["exclude_provider_id"] == "dead-1"
        return alive

    import app.routing.circuit_breaker as cb
    import app.routing.router as router
    monkeypatch.setattr(cb, "record_failure", fake_record_failure)
    monkeypatch.setattr(router, "select_provider", fake_select_provider)

    def start_stream(route):
        return _gen_from(EMPTY_STREAM if route.provider.id == "dead-1" else GOOD_STREAM)

    frames, gen, served = await stream_with_empty_guard(
        start_stream=start_stream, route=dead, db=None, hint=None,
        has_tools=False, has_images=False, key_type="standard",
        api_key_id="k1", model_override=None,
    )
    assert served is alive
    assert failures == ["dead-1"]
    replayed = frames + [f async for f in gen]
    assert replayed == GOOD_STREAM


@pytest.mark.asyncio
async def test_all_empty_raises_502(monkeypatch):
    dead1 = _FakeRoute("dead-1", "cursor-1")
    dead2 = _FakeRoute("dead-2", "cursor-2")
    failures = []

    async def fake_record_failure(pid, billing_error=False):
        failures.append(pid)

    routes = {"dead-1": dead1, "dead-2": dead2}

    async def fake_select_provider(db, hint, **kw):
        # Router keeps alternating between the two dead providers.
        return routes["dead-2" if kw["exclude_provider_id"] == "dead-1" else "dead-1"]

    import app.routing.circuit_breaker as cb
    import app.routing.router as router
    monkeypatch.setattr(cb, "record_failure", fake_record_failure)
    monkeypatch.setattr(router, "select_provider", fake_select_provider)

    with pytest.raises(HTTPException) as exc:
        await stream_with_empty_guard(
            start_stream=lambda r: _gen_from(EMPTY_STREAM),
            route=dead1, db=None, hint=None,
            has_tools=False, has_images=False, key_type="standard",
            api_key_id="k1", model_override=None,
        )
    assert exc.value.status_code == 502
    assert "empty-success" in exc.value.detail
    # Both dead providers accumulated breaker failures (≥2 each would
    # open default-threshold breakers and stop the ping-pong fleet-wide).
    assert failures.count("dead-1") >= 1 and failures.count("dead-2") >= 1


@pytest.mark.asyncio
async def test_reselected_already_failed_provider_gets_extra_failure(monkeypatch):
    # -s23 pair scenario: two same-priority dead cursor providers. When
    # selection bounces back to an already-empty-failed provider, the
    # guard records ANOTHER failure on it (pushes its breaker open) and
    # retries selection instead of re-streaming it.
    dead1 = _FakeRoute("dead-1", "cursor-1")
    dead2 = _FakeRoute("dead-2", "cursor-2")
    alive = _FakeRoute("alive-1", "google")
    failures = []
    selections = []

    async def fake_record_failure(pid, billing_error=False):
        failures.append(pid)

    seq = [dead2, dead1, alive]  # dead2 empty → reselect: dead1 (already failed) → alive

    async def fake_select_provider(db, hint, **kw):
        selections.append(kw["exclude_provider_id"])
        return seq.pop(0)

    import app.routing.circuit_breaker as cb
    import app.routing.router as router
    monkeypatch.setattr(cb, "record_failure", fake_record_failure)
    monkeypatch.setattr(router, "select_provider", fake_select_provider)

    def start_stream(route):
        return _gen_from(GOOD_STREAM if route.provider.id == "alive-1" else EMPTY_STREAM)

    frames, gen, served = await stream_with_empty_guard(
        start_stream=start_stream, route=dead1, db=None, hint=None,
        has_tools=False, has_images=False, key_type="standard",
        api_key_id="k1", model_override=None,
    )
    assert served is alive
    # dead-1: empty-failure + re-selection penalty; dead-2: empty-failure
    assert failures.count("dead-1") == 2
    assert failures.count("dead-2") == 1


@pytest.mark.asyncio
async def test_preflight_error_still_raises_http_status(monkeypatch):
    # The guard must not swallow the v3.10.13 pre-flight behavior.
    rt = _FakeRoute("p1", "p1")

    async def err_gen():
        yield _frame({"error": {"message": "invalid x-api-key"}})

    with pytest.raises(HTTPException) as exc:
        await stream_with_empty_guard(
            start_stream=lambda r: err_gen(), route=rt, db=None, hint=None,
            has_tools=False, has_images=False, key_type="standard",
            api_key_id="k1", model_override=None,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_healthy_stream_untouched(monkeypatch):
    rt = _FakeRoute("p1", "p1")
    frames, gen, served = await stream_with_empty_guard(
        start_stream=lambda r: _gen_from(GOOD_STREAM), route=rt, db=None,
        hint=None, has_tools=False, has_images=False, key_type="standard",
        api_key_id="k1", model_override=None,
    )
    assert served is rt
    assert frames + [f async for f in gen] == GOOD_STREAM
