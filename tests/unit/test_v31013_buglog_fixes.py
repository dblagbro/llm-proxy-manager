"""Tests for the v3.10.13 fixes — BUG-001 (streaming error contract),
BUG-037 (unregistered-model fast-fail), ARCH-A (pool-leak hardening)."""
from __future__ import annotations

import pytest

from app.api._messages_streaming import (
    _sse_frame_error, preflight_sse, http_status_for_stream_error,
)


# ── BUG-001: streaming error contract ────────────────────────────────────────

def test_sse_frame_error_detects_anthropic_error():
    frame = b'data: {"type": "error", "error": {"message": "invalid x-api-key"}}\n\n'
    assert _sse_frame_error(frame) == "invalid x-api-key"


def test_sse_frame_error_detects_openai_error():
    frame = b'data: {"error": "litellm.AuthenticationError: bad key"}\n\n'
    assert _sse_frame_error(frame) == "litellm.AuthenticationError: bad key"


def test_sse_frame_error_passes_anthropic_message_start():
    frame = (b'data: {"type":"message_start","message":{"id":"msg_proxy",'
             b'"type":"message","role":"assistant","content":[]}}\n\n')
    assert _sse_frame_error(frame) is None


def test_sse_frame_error_passes_openai_chunk():
    frame = (b'data: {"id":"x","object":"chat.completion.chunk",'
             b'"choices":[{"delta":{"role":"assistant"}}]}\n\n')
    assert _sse_frame_error(frame) is None


@pytest.mark.asyncio
async def test_preflight_sse_flags_pre_stream_error():
    async def g():
        yield b'data: {"type": "error", "error": {"message": "boom"}}\n\n'
        yield b'data: {"type":"message_stop"}\n\n'

    first, err, gen = await preflight_sse(g())
    assert err == "boom"
    assert b'"type": "error"' in first
    await gen.aclose()


@pytest.mark.asyncio
async def test_preflight_sse_passes_good_stream_and_is_replayable():
    async def g():
        yield b'data: {"type":"message_start","message":{}}\n\n'
        yield b'data: {"type":"content_block_delta"}\n\n'

    first, err, gen = await preflight_sse(g())
    assert err is None
    # the first frame plus the rest must reassemble into the whole stream
    seen = [first]
    async for c in gen:
        seen.append(c)
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_preflight_sse_empty_stream():
    async def g():
        return
        yield  # pragma: no cover — makes g() an async generator

    first, err, gen = await preflight_sse(g())
    assert err == "upstream produced an empty stream"


def test_http_status_for_stream_error():
    assert http_status_for_stream_error("litellm.AuthenticationError: invalid x-api-key") == 401
    assert http_status_for_stream_error("rate limit exceeded") == 429
    assert http_status_for_stream_error("upstream 503 service unavailable") == 502


# ── BUG-037: non-streaming claude-oauth timeout scaling ──────────────────────

def test_oauth_complete_timeout_scales_with_max_tokens():
    from app.api._messages_streaming import _oauth_complete_timeout
    # a tiny request (e.g. an unregistered model that routes here and
    # hangs) is bounded near the 90s floor — not the old flat 300s.
    assert _oauth_complete_timeout(10).read < 95.0
    # a genuinely large generation still gets the full ceiling.
    assert _oauth_complete_timeout(100_000).read == 300.0
    # mid-size scales between floor and ceiling.
    assert 90.0 < _oauth_complete_timeout(4096).read < 300.0
    # degenerate inputs fall back to the floor, never below it.
    assert _oauth_complete_timeout(0).read == 90.0
    assert _oauth_complete_timeout(None).read == 90.0
    # connect timeout stays small (the 2026-05-05 outage fix).
    assert _oauth_complete_timeout(4096).connect == 5.0


# ── ARCH-A: supervisor must not hold a DB connection across the LLM call ─────

@pytest.mark.asyncio
async def test_review_one_provider_releases_db_before_llm_call(monkeypatch):
    """ARCH-A — review_one_provider must commit (return the pooled
    connection) BEFORE classify_with_llm, not hold it across the call."""
    import app.monitoring.ai_provider_supervisor as sup
    import app.monitoring.ai_provider_supervisor_stats as stats_mod

    events: list[str] = []

    class FakeDB:
        async def commit(self):
            events.append("commit")
        async def flush(self):
            events.append("flush")
        def add(self, obj):
            events.append("add")

    async def fake_stats(db, pid, **kw):
        return {"short_window": {"requests": 5}}

    async def fake_classify(name, ptype, stats):
        events.append("classify")
        return None  # returns before the write block — keeps the test small

    monkeypatch.setattr(stats_mod, "compute_provider_stats", fake_stats)
    monkeypatch.setattr(sup, "classify_with_llm", fake_classify)

    class _P:
        id = "p1"
        name = "P1"
        provider_type = "openai"
        manual_override_until = None

    await sup.review_one_provider(FakeDB(), _P())
    assert "commit" in events and "classify" in events
    assert events.index("commit") < events.index("classify"), (
        "the DB connection must be released before the LLM call (ARCH-A)"
    )
