"""v5.7.17 — client-disconnect watchdog dependency.

The root-cause for the 2026-06-16 tmrwww01 DB pool leak: when a client
(e.g. the AI supervisor probe) disconnected mid-request — usually due
to httpx timeout — FastAPI/Starlette did NOT auto-cancel the handler.
The handler kept running, kept holding its ``async with db: ...``
connection, and the pool slot stayed pinned until the upstream
eventually responded (or the handler crashed). One leaked slot per
abandoned request; the supervisor sweeps every 30 min and an upstream
slowdown can produce a dozen abandoned requests before the operator
notices ``/health.dbPool.checked_out`` climbing.

This watchdog is a FastAPI dependency that runs a polling task in
parallel with the handler. When ``request.is_disconnected()`` returns
true, the watchdog cancels the handler task. ``asyncio.CancelledError``
propagates through the handler's ``async with`` blocks, the DB
connection is released, and FastAPI returns 499 to the (already-gone)
client. Pool stays clean.

Defaults: enabled, 2s poll interval. Disable via
``DISCONNECT_WATCHDOG_ENABLED=false`` if a regression surfaces (the
flag exists so we can pin the watchdog OFF and confirm the pool leak
returns — a clean A/B repro).
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from fastapi import Request

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    try:
        from app.config import settings
        return bool(getattr(settings, "disconnect_watchdog_enabled", True))
    except Exception:
        return True


def _poll_interval_sec() -> float:
    try:
        from app.config import settings
        return float(getattr(settings, "disconnect_watchdog_interval_sec", 2.0))
    except Exception:
        return 2.0


async def watch_for_disconnect(request: Request) -> AsyncIterator[None]:
    """FastAPI ``yield`` dependency. Spawns a watcher task that polls
    ``request.is_disconnected()`` and, on disconnect, cancels the
    handler task — which releases the DB connection via the
    ``async with`` context in ``get_db``.

    No-op when the flag is off OR when running outside a request context
    (e.g. unit tests without a TestClient). Failures inside the watcher
    are swallowed — this is reliability instrumentation, never gates the
    request.
    """
    if not _enabled():
        yield
        return

    main_task = asyncio.current_task()
    if main_task is None:
        # Defensive: no running task means no one to cancel.
        yield
        return

    stop = asyncio.Event()
    interval = _poll_interval_sec()
    # v5.21.11 — handler_done flag closes the LIFO cleanup race that
    # caused the /cluster/sync inbound DB-session leak. When a peer
    # POST completes, ASGI queues an http.disconnect and
    # ``request.is_disconnected()`` returns True. The pre-v5.21.11
    # watcher would then cancel main_task — but main_task was already
    # in FastAPI's dependency-cleanup phase, running ``get_db``'s
    # ``async with __aexit__``. The cancel interrupted
    # ``session.close()`` mid-way, so the pool slot never returned. In
    # LIFO order the watchdog dep is popped AFTER the db dep, so
    # ``stop.set()`` in the finally-below runs too late to save that
    # cleanup. The flag is the earliest signal we can send: the
    # handler yield has returned, so any "disconnected" observation
    # from here on is post-handler and MUST NOT cancel. Set inside
    # the yield-adjacent try/finally so it lands before get_db's
    # __aexit__ starts.
    handler_done = [False]

    async def _watcher() -> None:
        # First check is short — catch fast disconnects (sub-second
        # client aborts during request setup).
        try:
            await asyncio.sleep(min(0.5, interval))
        except asyncio.CancelledError:
            return
        while not stop.is_set():
            if handler_done[0]:
                return
            try:
                disconnected = await request.is_disconnected()
            except Exception:
                # A failed disconnect check shouldn't cancel a working
                # request. Pause and retry.
                disconnected = False
            # Recheck after the await — handler_done may have flipped
            # while we were awaiting is_disconnected(). If it has, the
            # disconnected observation is post-handler noise and must
            # not cancel cleanup.
            if handler_done[0] or stop.is_set():
                return
            if disconnected:
                logger.info(
                    "disconnect_watchdog.client_gone cancelling handler "
                    "path=%s", request.url.path,
                )
                main_task.cancel()
                return
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

    watcher_task = asyncio.create_task(
        _watcher(), name="disconnect-watchdog"
    )
    try:
        yield
    finally:
        # Order matters. Set handler_done FIRST so any concurrent
        # watcher tick that's about to cancel sees the flag and
        # bails. Then stop.set() for the sleep-loop exit path. Only
        # then cancel/await the watcher task. If watcher already
        # cancelled main_task before we got here, that cancel is
        # legitimate (handler was still running) and the exception
        # will propagate through this finally.
        handler_done[0] = True
        stop.set()
        if not watcher_task.done():
            watcher_task.cancel()
            try:
                await watcher_task
            except (asyncio.CancelledError, Exception):
                pass
