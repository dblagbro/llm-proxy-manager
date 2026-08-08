"""v5.21.3 — Buffered-cascade streaming with SSE heartbeats.

Opt-in via ``refusal_retry_streaming_heartbeat`` column. When set,
the buffered-cascade path routes through
``_buffered_cascade_stream.run_buffered_cascade_stream_with_heartbeat``
which returns a ``StreamingResponse`` at the top of the handler and
yields ``: keepalive`` SSE comment frames during the buffered
dispatch, so the client's connection reads as alive.

Trade-off (documented): heartbeat mode runs a MINIMAL dispatch —
LLM call + optional cascade only. No tool hops, no memory extraction,
no MCP injection, no response tail. Full-feature dispatch stays on
the v5.21.0 no-heartbeat path.
"""
from __future__ import annotations

import asyncio
from pathlib import Path


def test_helper_module_present():
    src_path = Path("app/api/_buffered_cascade_stream.py")
    assert src_path.exists()
    src = src_path.read_text()
    assert "async def run_buffered_cascade_stream_with_heartbeat" in src


def test_schema_migration_column_present():
    src = Path("app/models/database.py").read_text()
    assert "ADD COLUMN refusal_retry_streaming_heartbeat INTEGER DEFAULT 0" in src


def test_orm_column_declared():
    src = Path("app/models/db_apikey.py").read_text()
    assert "refusal_retry_streaming_heartbeat = Column" in src


def test_messages_handler_delegates_when_heartbeat_flag_set():
    src = Path("app/api/messages.py").read_text()
    assert "_buffered_cascade_heartbeat" in src
    assert "run_buffered_cascade_stream_with_heartbeat" in src
    # Mode header carries the distinction
    assert '"buffered-heartbeat"' in src
    assert '"buffered"' in src


def test_heartbeat_flag_gates_on_retry_enabled():
    """The heartbeat flag ALONE isn't enough — refusal_retry_enabled
    must also be True. Otherwise a caller who set the heartbeat
    column but never enabled retry would get an SSE stream from a
    non-cascade dispatch, which is nonsense."""
    src = Path("app/api/messages.py").read_text()
    # _buffered_cascade_heartbeat is defined AS a chain from
    # _buffered_cascade_stream (which requires refusal_retry_enabled)
    assert "_buffered_cascade_heartbeat = _buffered_cascade_stream and" in src


def test_helper_emits_initial_marker_frame():
    """The initial ``: cascade-buffering`` frame kicks bytes to the
    client immediately so the connection doesn't read as idle before
    the first heartbeat interval elapses."""
    src = Path("app/api/_buffered_cascade_stream.py").read_text()
    assert 'b": cascade-buffering\\n\\n"' in src
    assert "_INITIAL_FRAME" in src


def test_helper_emits_periodic_heartbeat_frames():
    src = Path("app/api/_buffered_cascade_stream.py").read_text()
    assert 'b": keepalive\\n\\n"' in src
    assert "_HEARTBEAT_FRAME" in src
    # And the loop that yields them:
    assert "asyncio.wait_for" in src
    assert "asyncio.TimeoutError" in src


def test_helper_uses_asyncio_shield():
    """Without ``asyncio.shield``, wait_for cancels the underlying task
    on timeout — which would KILL the dispatch and force it to restart.
    Shield is load-bearing."""
    src = Path("app/api/_buffered_cascade_stream.py").read_text()
    assert "asyncio.shield" in src


def test_helper_surfaces_dispatch_errors_as_sse_error_frame():
    """Never let a stack trace leak into the stream — the client is
    parsing SSE, not Python."""
    src = Path("app/api/_buffered_cascade_stream.py").read_text()
    assert '"internal_error"' in src


def test_helper_reuses_cascade_module():
    """Heartbeat mode still runs the v5.20.1 cascade — the whole point
    of ``refusal_retry_enabled`` is the retry chain. Skipping cascade
    would defeat the flag."""
    src = Path("app/api/_buffered_cascade_stream.py").read_text()
    assert "maybe_cascade_on_refusal" in src


def test_helper_converts_result_to_anthropic_sse_frames():
    src = Path("app/api/_buffered_cascade_stream.py").read_text()
    for helper in ("anthropic_text_sse", "anthropic_tool_sse", "anthropic_tools_sse"):
        assert helper in src


def test_heartbeat_interval_is_configurable():
    """Hardcoding N seconds would make the module untestable. The
    generator should accept an ``interval_seconds`` param with a
    sensible default. Verifies the default is documented as a
    module-level constant."""
    src = Path("app/api/_buffered_cascade_stream.py").read_text()
    assert "DEFAULT_HEARTBEAT_INTERVAL_SEC" in src
    assert "interval_seconds:" in src


def test_helper_produces_valid_sse_via_short_dispatch():
    """End-to-end (async) — mock the dispatch to return quickly and
    verify the generator yields the initial marker + at least one
    real SSE frame at the end."""
    from unittest.mock import patch

    class _Route:
        litellm_model = "openai/gpt-4o"

    class _Key:
        refusal_retry_enabled = True

    async def _fake_dispatch(**kwargs):
        # Skip the real cascade module — just return a valid anthropic result
        return {
            "content": [{"type": "text", "text": "hello world"}],
        }

    async def _drive():
        with patch(
            "app.api._buffered_cascade_stream._dispatch_with_cascade",
            side_effect=_fake_dispatch,
        ):
            from app.api._buffered_cascade_stream import (
                run_buffered_cascade_stream_with_heartbeat,
            )
            frames: list[bytes] = []
            async for chunk in run_buffered_cascade_stream_with_heartbeat(
                route=_Route(),
                key_record=_Key(),
                messages_list=[{"role": "user", "content": "hi"}],
                extra={},
                system=None,
                max_tokens=100,
                has_images=False,
                hint=None,
                db=None,
                interval_seconds=0.05,
            ):
                frames.append(chunk)
            return frames

    frames = asyncio.run(_drive())
    joined = b"".join(frames)
    # Initial marker first
    assert frames[0] == b": cascade-buffering\n\n"
    # Real content shows up
    assert b"content_block_start" in joined
    assert b"hello world" in joined
    assert b"[DONE]" in joined


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 21, 3), (
        f"expected >= 5.21.3, got {major}.{minor}.{patch}"
    )
