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
    """The cluster_sync handler MUST list ``Depends(watch_for_disconnect)``
    before ``Depends(get_db)`` so the watcher is armed before the session
    is checked out.
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
        "v5.9.10: cluster_sync signature must include "
        "Depends(watch_for_disconnect)."
    )
    assert "Depends(get_db)" in signature, "signature should still take db"

    watchdog_idx = signature.find("Depends(watch_for_disconnect)")
    db_idx = signature.find("Depends(get_db)")
    assert 0 < watchdog_idx < db_idx, (
        "v5.9.10: watchdog must be listed before db in the cluster_sync "
        "signature (else a fast disconnect leaks the session)."
    )


def test_version_bumped():
    src = Path("app/__version__.py").read_text()
    assert '"5.9.10"' in src, "__version__ should be 5.9.10"
