"""v3.10.17 — hedge-correctness fix for race_streams.

A stream now "wins" the hedge race only if its first chunk is *healthy*
(not a terminal SSE error frame, not an empty stream). A fast-failing
primary no longer beats a healthy backup.
"""
from __future__ import annotations

import pytest

from app.routing.hedging import race_streams, _chunk_ok
from app.api._messages_streaming import preflight_sse


def _gen_factory(*frames: bytes):
    def factory():
        async def g():
            for f in frames:
                yield f
        return g()
    return factory


_ERR = b'data: {"type": "error", "error": {"message": "%s"}}\n\n'
_OK = b'data: {"type":"message_start"}\n\n'


def test_chunk_ok_classification():
    assert _chunk_ok(_OK) is True
    assert _chunk_ok(_ERR % b"down") is False
    assert _chunk_ok(None) is False  # empty stream


@pytest.mark.asyncio
async def test_race_skips_failing_primary_for_healthy_backup():
    """The core fix — a primary that fast-fails pre-stream must NOT win
    the race over a healthy backup."""
    primary = _gen_factory(_ERR % b"401 invalid key")
    backup = _gen_factory(_OK, b'data: more\n\n')

    racer, winner = await race_streams(primary, backup, wait_ms=50)
    assert winner == "backup", "healthy backup must win over a failing primary"
    first, err, racer = await preflight_sse(racer)
    assert err is None, "the winning (backup) stream is healthy"
    await racer.aclose()


@pytest.mark.asyncio
async def test_race_healthy_primary_still_wins():
    """Regression guard — a healthy primary still wins outright."""
    primary = _gen_factory(_OK, b'data: y\n\n')
    backup = _gen_factory(_OK)
    racer, winner = await race_streams(primary, backup, wait_ms=50)
    assert winner == "primary"
    await racer.aclose()


@pytest.mark.asyncio
async def test_race_empty_primary_counts_as_failure():
    """An empty primary stream counts as a failure — backup wins."""
    primary = _gen_factory()  # yields nothing
    backup = _gen_factory(_OK)
    racer, winner = await race_streams(primary, backup, wait_ms=50)
    assert winner == "backup"
    await racer.aclose()


@pytest.mark.asyncio
async def test_race_both_fail_returns_primary_failure():
    """When both branches fail, primary's failed stream is returned so
    the caller's preflight_sse turns it into a real HTTP status."""
    primary = _gen_factory(_ERR % b"primary down")
    backup = _gen_factory(_ERR % b"backup down")
    racer, winner = await race_streams(primary, backup, wait_ms=50)
    first, err, racer = await preflight_sse(racer)
    assert err is not None and "primary down" in err
    await racer.aclose()
