"""v5.21.12 — pool-leak root-cause fix pins.

The v5.21.11 handler_done flag was necessary but not sufficient. FastAPI
dep cleanup is LIFO, so when ``Depends(watch_for_disconnect)`` is
declared BEFORE ``Depends(get_db)``, the watcher is still polling
during ``get_db.__aexit__``. Setting handler_done inside the
watchdog's finally can't help because that finally runs AFTER get_db's
finally — the race is already lost by then.

v5.21.12 fixes it in two places:

1. **Dep-order swap on /cluster/sync**: db FIRST, watchdog SECOND. LIFO
   cleanup puts db-cleanup LAST (after watchdog stops), so the watcher
   is guaranteed dead before session.close() is awaited.

2. **asyncio.shield on get_db cleanup**: defense-in-depth. Any
   Depends(get_db) user with a broken dep order (or a cancel from
   elsewhere) still gets safe cleanup — shield defers the cancel
   until close() completes.

Together they close the root-cause window that produced 1 leaked
session per inbound /cluster/sync POST → chronic ~1/2min TMR pool
climb → login failure every ~90min.
"""
from __future__ import annotations
from pathlib import Path


def test_cluster_sync_dep_order_db_before_watchdog():
    """/cluster/sync must declare db BEFORE watchdog so LIFO puts
    db cleanup last (after watcher stops)."""
    src = Path("app/api/cluster.py").read_text()
    # Find the cluster_sync signature
    idx_sig = src.find("async def cluster_sync(")
    assert idx_sig > 0, "cluster_sync handler not found"
    # Look up to 2000 chars into the signature
    sig = src[idx_sig:idx_sig + 2000]
    idx_db = sig.find("Depends(get_db)")
    idx_watchdog = sig.find("Depends(watch_for_disconnect)")
    assert idx_db > 0 and idx_watchdog > 0, (
        "expected both Depends(get_db) and Depends(watch_for_disconnect) "
        f"in cluster_sync signature; got db={idx_db} watchdog={idx_watchdog}"
    )
    assert idx_db < idx_watchdog, (
        f"cluster_sync dep order wrong: db at {idx_db}, watchdog at "
        f"{idx_watchdog} — db MUST come first so LIFO cleanup puts it "
        "LAST (safe from watcher-triggered cancel)"
    )


def test_get_db_uses_asyncio_shield():
    """get_db.__aexit__ MUST be wrapped in asyncio.shield so
    session.close() completes even if the request task is cancelled
    mid-await."""
    src = Path("app/models/database.py").read_text()
    idx = src.find("async def get_db(")
    assert idx > 0
    body = src[idx:idx + 3000]
    assert "asyncio.shield" in body or "_asyncio.shield" in body, (
        "get_db must use asyncio.shield around session.__aexit__ so "
        "cancellation during cleanup can't leak the pool slot"
    )
    # And the shield must be around the __aexit__ / close call, not
    # some other await.
    # Find shield location + verify __aexit__ appears near it.
    shield_idx = body.find("shield(")
    assert shield_idx > 0
    window = body[shield_idx:shield_idx + 400]
    assert "__aexit__" in window, (
        "shield must be wrapped around __aexit__ (not some other await)"
    )


def test_get_db_preserves_operational_error_swallow():
    """The v3.7.21 documented swallow of ``no active connection`` on
    post-cancellation must still work."""
    src = Path("app/models/database.py").read_text()
    idx = src.find("async def get_db(")
    body = src[idx:idx + 5000]
    assert "OperationalError" in body
    assert '"no active connection"' in body


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 21, 12), (
        f"expected >= 5.21.12, got {major}.{minor}.{patch}"
    )
