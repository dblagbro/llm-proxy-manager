"""v5.21.11 — disconnect_watchdog LIFO cleanup race regression pin.

Reproduces the /cluster/sync inbound DB-pool leak:

    for i, peer_post in enumerate(inbound_posts):
        # handler runs, returns 200
        # LIFO cleanup: db.__aexit__ starts (async, awaits session.close)
        # meanwhile client (peer) closed connection → is_disconnected() = True
        # pre-v5.21.11 watcher: sees disconnected → main_task.cancel()
        # cancel interrupts session.close() → pool slot never returned
        # net effect: 1 leaked session per inbound POST

The v5.21.11 fix adds a ``handler_done`` flag that the watcher checks
BEFORE cancelling. Setting it in the yield-finally (before stop.set()
+ watcher_task.cancel()) closes the window: any tick that fires after
the handler returns sees the flag and bails without cancelling.

The regression risk if this fix ever gets reverted or the flag write
gets reordered: /cluster/sync inbound leaks 1 DB session per peer POST
again → chronic ~2/24h fill on TMR nodes → login stops working
mid-day.
"""
from __future__ import annotations
from pathlib import Path


def test_handler_done_flag_present():
    src = Path("app/utils/disconnect_watchdog.py").read_text()
    assert "handler_done = [False]" in src, (
        "handler_done flag is missing — watcher will cancel during "
        "post-handler cleanup and leak DB sessions"
    )


def test_handler_done_set_before_stop():
    """The order matters: handler_done MUST be set BEFORE stop.set()
    so any watcher tick that races the yield-finally sees the flag
    (not just stop) and bails without cancelling.

    Match STATEMENTS (indented, not in comments): the pattern
    ``\\n        <expr>`` skips any occurrence embedded in a
    docstring or ``#`` comment line."""
    src = Path("app/utils/disconnect_watchdog.py").read_text()
    idx_done = src.find("\n        handler_done[0] = True")
    idx_stop = src.find("\n        stop.set()")
    idx_cancel = src.find("\n            watcher_task.cancel()")
    assert 0 < idx_done < idx_stop < idx_cancel, (
        f"finally-block order wrong: handler_done={idx_done} "
        f"stop.set={idx_stop} cancel={idx_cancel} "
        "(expected handler_done < stop < cancel as executable statements)"
    )


def test_watcher_rechecks_after_await():
    """Post-await recheck is what closes the race: watcher awaits
    is_disconnected(), which yields control; the yield-finally can run
    during that window and set handler_done. Without the recheck the
    watcher would still cancel."""
    src = Path("app/utils/disconnect_watchdog.py").read_text()
    # Find the disconnected-check block
    idx = src.find("disconnected = await request.is_disconnected()")
    assert idx > 0
    # Between the await and the `if disconnected:` the flag must be rechecked
    window = src[idx:idx + 800]
    assert "if handler_done[0] or stop.is_set():" in window, (
        "watcher must recheck handler_done after the is_disconnected() "
        "await, before the cancel-decision"
    )


def test_early_return_on_handler_done():
    """The watcher's while-loop head should also check handler_done
    so we exit cleanly if the flag is set between iterations."""
    src = Path("app/utils/disconnect_watchdog.py").read_text()
    idx = src.find("while not stop.is_set():")
    assert idx > 0
    body_start = src.find("\n", idx) + 1
    body_snippet = src[body_start:body_start + 200]
    assert "if handler_done[0]:" in body_snippet


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 21, 11), (
        f"expected >= 5.21.11, got {major}.{minor}.{patch}"
    )
