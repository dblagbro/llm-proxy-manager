"""v5.7.17 — client-disconnect watchdog: fix supervisor DB pool leak.

The watchdog runs in parallel with each /v1/messages and
/v1/chat/completions handler. On client disconnect, it cancels the
handler task — ``CancelledError`` propagates through ``async with db:``
and releases the DB connection. Closes the 2026-06-16 supervisor
pool-leak path.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── structural pins ────────────────────────────────────────────────────


def test_watchdog_module_exists():
    from app.utils.disconnect_watchdog import watch_for_disconnect  # noqa: F401


def test_messages_handler_wires_watchdog():
    """The handler MUST depend on watch_for_disconnect, and it MUST be
    listed BEFORE the ``db`` param so the watchdog is set up before any
    DB session is checked out — otherwise on a fast disconnect the
    connection could leak in the gap."""
    src = Path("app/api/messages.py").read_text()
    assert "from app.utils.disconnect_watchdog import watch_for_disconnect" in src
    assert "Depends(watch_for_disconnect)" in src
    # Order check
    watchdog_idx = src.find("Depends(watch_for_disconnect)")
    db_idx = src.find("Depends(get_db)")
    assert 0 < watchdog_idx < db_idx, (
        "v5.7.17: watchdog must be listed before db in messages() signature."
    )


def test_completions_handler_wires_watchdog():
    """Same contract for /v1/chat/completions."""
    src = Path("app/api/completions.py").read_text()
    assert "from app.utils.disconnect_watchdog import watch_for_disconnect" in src
    assert "Depends(watch_for_disconnect)" in src
    watchdog_idx = src.find("Depends(watch_for_disconnect)")
    db_idx = src.find("Depends(get_db)")
    assert 0 < watchdog_idx < db_idx, (
        "v5.7.17: watchdog must be listed before db in chat_completions() signature."
    )


def test_settings_exposed():
    from app.config import settings
    assert hasattr(settings, "disconnect_watchdog_enabled")
    assert hasattr(settings, "disconnect_watchdog_interval_sec")


def test_default_enabled_and_2s_interval():
    """Defaults: ON (this IS the operator-asked production fix), 2s
    interval. Anything tighter (sub-second) wastes CPU; anything
    looser (>10s) lets the pool leak window grow back."""
    from app.config import settings
    assert settings.disconnect_watchdog_enabled is True
    assert 0.5 <= settings.disconnect_watchdog_interval_sec <= 5.0


def test_version_bumped():
    """v5.7.17 minimum — later patches keep this passing."""
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (5, 7, 17), f"v5.7.17 must be reachable; got {__version__}"


# ── behavioural pins ───────────────────────────────────────────────────


def _fake_request(disconnect_on_call=None):
    """Build a Request stub. ``disconnect_on_call=None`` → never
    disconnects (returns False forever). ``disconnect_on_call=N`` →
    returns True starting from the N-th call. Use 1 to disconnect on
    the first poll."""
    req = MagicMock()
    req.url.path = "/v1/messages"
    state = {"calls": 0}

    async def is_disconnected():
        state["calls"] += 1
        if disconnect_on_call is None:
            return False
        return state["calls"] >= disconnect_on_call

    req.is_disconnected = is_disconnected
    return req, state


@pytest.mark.asyncio
async def test_watchdog_is_noop_when_disabled():
    """Flag OFF → dependency yields immediately and spawns no task."""
    from app.utils import disconnect_watchdog as dw
    req, state = _fake_request(disconnect_on_call=None)
    with patch.object(dw, "_enabled", lambda: False):
        gen = dw.watch_for_disconnect(req)
        v = await gen.__anext__()
        assert v is None
        # No watcher means no is_disconnected calls.
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
    assert state["calls"] == 0


@pytest.mark.asyncio
async def test_watchdog_clean_handler_no_cancel():
    """When client never disconnects, the watcher polls a few times
    then exits cleanly when the dependency's finally block fires. The
    handler task is NOT cancelled."""
    from app.utils import disconnect_watchdog as dw
    req, state = _fake_request(disconnect_on_call=None)

    main_task = asyncio.current_task()
    assert main_task is not None

    with patch.object(dw, "_enabled", lambda: True), \
         patch.object(dw, "_poll_interval_sec", lambda: 0.01):
        gen = dw.watch_for_disconnect(req)
        await gen.__anext__()
        # Let the watcher tick a few times.
        await asyncio.sleep(0.05)
        # Close the dependency — simulates handler finishing normally.
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    assert not main_task.cancelled(), "watchdog falsely cancelled a healthy handler"
    assert state["calls"] > 0, "watcher never polled is_disconnected"


@pytest.mark.asyncio
async def test_watchdog_cancels_handler_on_disconnect():
    """When is_disconnected() returns True, the watcher cancels the
    main task. We model this by spawning the work as a child task and
    asserting the cancel propagates."""
    from app.utils import disconnect_watchdog as dw

    req, state = _fake_request(disconnect_on_call=1)
    cancelled = asyncio.Event()

    async def handler():
        gen = dw.watch_for_disconnect(req)
        await gen.__anext__()
        try:
            # Simulate the real handler awaiting upstream
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            try:
                await gen.__anext__()
            except (StopAsyncIteration, asyncio.CancelledError):
                pass

    with patch.object(dw, "_enabled", lambda: True), \
         patch.object(dw, "_poll_interval_sec", lambda: 0.01):
        task = asyncio.create_task(handler())
        try:
            await asyncio.wait_for(asyncio.wait({task}), timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            raise AssertionError("watchdog did not cancel handler on disconnect")

    assert cancelled.is_set(), (
        "v5.7.17: handler did not receive CancelledError after disconnect"
    )


@pytest.mark.asyncio
async def test_is_disconnected_exception_does_not_cancel():
    """If is_disconnected() itself raises (e.g. connection partially
    closed), the watcher must NOT mistakenly cancel a healthy handler.
    Defensive: a flaky probe shouldn't kill working requests."""
    from app.utils import disconnect_watchdog as dw

    req = MagicMock()
    req.url.path = "/v1/messages"

    async def is_disconnected():
        raise RuntimeError("transient probe failure")

    req.is_disconnected = is_disconnected

    main_task = asyncio.current_task()
    with patch.object(dw, "_enabled", lambda: True), \
         patch.object(dw, "_poll_interval_sec", lambda: 0.01):
        gen = dw.watch_for_disconnect(req)
        await gen.__anext__()
        await asyncio.sleep(0.05)
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    assert not main_task.cancelled(), (
        "v5.7.17: a flaky is_disconnected() must not cancel the handler"
    )
