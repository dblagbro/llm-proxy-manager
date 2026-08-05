"""v5.9.10 — extend the v5.7.17 disconnect watchdog to /cluster/sync.

Background: caught a slow DB pool leak on tmrwww02 on 2026-06-27. The
node receives ~1088 inbound /cluster/sync POSTs per 24h (from peers
pushing state). ~0.2% of those calls were leaving a DB session checked
out — about 2-8 sessions/day, plateauing at 8 before container restart.
The `_watchdog` extension that v5.9.9 wired into responses/audio/images
didn't cover the cluster sync handler because cluster.py:181 only takes
`db = Depends(get_db)` and not the watchdog.

Root cause: peer-side httpx timeout or network blip mid-`apply_sync`
leaves the FastAPI handler running with the DB session held. Same shape
as the v5.7.17 supervisor leak, different endpoint.

This is a pure structural pin — identical contract to the v5.7.17 +
v5.9.9 tests: the watchdog dep MUST appear before `db = Depends(get_db)`
in the handler signature so the watcher is armed before any session is
checked out.
"""
from __future__ import annotations

from pathlib import Path


def test_cluster_sync_imports_watchdog():
    src = Path("app/api/cluster.py").read_text()
    assert "from app.utils.disconnect_watchdog import watch_for_disconnect" in src, (
        "v5.9.10: app/api/cluster.py must import the disconnect watchdog."
    )


def test_cluster_sync_wires_watchdog_before_db():
    """The cluster_sync handler MUST list ``Depends(get_db)`` BEFORE
    ``Depends(watch_for_disconnect)``.

    v5.21.12 REVERSED the original v5.9.10 order — that ordering was the
    leak. FastAPI tears down yield-deps LIFO, so db-first makes get_db
    close LAST (after the watcher stops), which is what actually fixed the
    chronic /cluster/sync pool-session leak. This was the FIRST route fixed;
    v5.21.14 propagated the same db-first order to the six remaining
    watchdog routes. (Test name kept for history; the assertion now pins
    the corrected order.)
    """
    src = Path("app/api/cluster.py").read_text()
    handler_anchor = src.find("async def cluster_sync(")
    assert handler_anchor > 0, "cluster_sync handler not found"

    # Find the close-paren of the signature, then bound our search there.
    # Signatures span multiple lines; find the next closing ) on its own.
    sig_end = src.find("\n):", handler_anchor)
    assert sig_end > handler_anchor, "couldn't bound cluster_sync signature"
    signature = src[handler_anchor:sig_end]

    assert "Depends(watch_for_disconnect)" in signature, (
        "cluster_sync signature must include Depends(watch_for_disconnect)."
    )
    assert "Depends(get_db)" in signature, "signature should still take db"

    watchdog_idx = signature.find("Depends(watch_for_disconnect)")
    db_idx = signature.find("Depends(get_db)")
    assert 0 < db_idx < watchdog_idx, (
        "v5.21.12: db must be listed before watchdog in the cluster_sync "
        "signature (LIFO cleanup closes get_db last — see v5.21.14)."
    )


def test_version_bumped():
    """v5.21.12+ — this feature shipped at 5.9.10; assert we're at least
    there rather than pinning an exact ancient version (which would fail
    on every subsequent release)."""
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m, "no version string found"
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) >= (5, 9, 10)
