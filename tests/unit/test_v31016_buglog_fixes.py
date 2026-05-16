"""v3.10.16 — BUG-001 hedged-path follow-up.

The hedged streaming path built its StreamingResponse straight from
``race_streams`` output, so a pre-stream upstream failure on the winning
branch still rode back as HTTP 200 + a terminal SSE error frame. The
hedged path now pre-flights the racer (parity with the non-hedged path,
fixed in v3.10.13). These tests exercise the *real* ``race_streams`` +
``preflight_sse`` together.
"""
from __future__ import annotations

import pytest

from app.routing.hedging import race_streams
from app.api._messages_streaming import preflight_sse


def _gen_factory(*frames: bytes):
    def factory():
        async def g():
            for f in frames:
                yield f
        return g()
    return factory


@pytest.mark.asyncio
async def test_bug001_hedged_preflight_catches_pre_stream_error():
    """When BOTH hedged branches fail pre-stream, race_streams returns
    primary's failed stream and preflight_sse surfaces it as a real
    error (instead of a 200 + error frame). (v3.10.17: a single failing
    branch no longer reaches here — the healthy branch wins the race.)"""
    primary = _gen_factory(
        b'data: {"type": "error", "error": {"message": "invalid x-api-key"}}\n\n',
        b'data: {"type":"message_stop"}\n\n',
    )
    backup = _gen_factory(
        b'data: {"type": "error", "error": {"message": "backup also down"}}\n\n',
    )

    racer, winner = await race_streams(primary, backup, wait_ms=50)
    first, err, racer = await preflight_sse(racer)
    assert err is not None and "x-api-key" in err
    await racer.aclose()


@pytest.mark.asyncio
async def test_bug001_hedged_preflight_passes_good_stream():
    """A healthy hedged stream pre-flights clean and stays replayable."""
    primary = _gen_factory(
        b'data: {"type":"message_start"}\n\n',
        b'data: {"type":"content_block_delta"}\n\n',
    )
    backup = _gen_factory(b'data: {"type":"message_start"}\n\n')

    racer, winner = await race_streams(primary, backup, wait_ms=50)
    first, err, racer = await preflight_sse(racer)
    assert err is None
    seen = [first]
    async for c in racer:
        seen.append(c)
    assert len(seen) == 2, "first frame + the rest must reassemble the whole stream"
