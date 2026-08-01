"""v5.9.9 — extend the v5.7.17 disconnect watchdog to /v1/responses,
/v1/audio/{speech,transcriptions}, and /v1/images/generations.

Background: v5.7.17 wired ``watch_for_disconnect`` into /v1/messages,
/v1/chat/completions, and the /api/integration/chat handler — but the
audio + images endpoints added in v5.9.0 and the older /v1/responses
shim were never updated, so they still leak a DB pool slot when the
caller disconnects mid-handler. www2 caught one in the wild:
``/health.dbPool.oldest_checkout_age_sec ≈ 41891`` (11.6h) with the
container otherwise idle.

These are pure structural pins — identical contract to
``test_v5717_disconnect_watchdog.py``. v5.21.14 REVERSED the ordering:
``db = Depends(get_db)`` MUST appear BEFORE the watchdog dep. FastAPI
tears down yield-deps LIFO, so db-first makes get_db close LAST (after
the watcher has stopped), which is what stops the client-disconnect
cancel from interrupting session.close() and leaking a pool slot. The
original v5.7.17 "arm before checkout" order was itself the leak — see
cluster.py v5.21.12 and the v5.21.14 changelog.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "path",
    [
        "app/api/responses.py",
        "app/api/audio.py",
        "app/api/images.py",
    ],
)
def test_module_imports_watchdog(path: str):
    src = Path(path).read_text()
    assert "from app.utils.disconnect_watchdog import watch_for_disconnect" in src, (
        f"v5.9.9: {path} must import the watchdog dependency."
    )


@pytest.mark.parametrize(
    "path,n_handlers",
    [
        ("app/api/responses.py", 1),       # /v1/responses
        ("app/api/audio.py", 2),           # speech + transcriptions
        ("app/api/images.py", 1),          # generations
    ],
)
def test_each_handler_wires_watchdog_before_db(path: str, n_handlers: int):
    """v5.21.14 — every handler that takes ``db = Depends(get_db)`` MUST
    also take ``_watchdog = Depends(watch_for_disconnect)`` listed AFTER
    it (db-first), so FastAPI's LIFO cleanup closes get_db last.

    We assert per-handler ordering by walking the file and pairing each
    ``Depends(get_db)`` with the nearest FOLLOWING
    ``Depends(watch_for_disconnect)`` — that pairing must exist within
    the same signature.
    """
    src = Path(path).read_text()
    db_positions = _all_positions(src, "Depends(get_db)")
    wd_positions = _all_positions(src, "Depends(watch_for_disconnect)")

    assert len(db_positions) == n_handlers, (
        f"{path}: expected {n_handlers} handlers using get_db, "
        f"found {len(db_positions)}"
    )
    assert len(wd_positions) >= n_handlers, (
        f"{path}: expected at least {n_handlers} watchdog deps, "
        f"found {len(wd_positions)}"
    )

    # Pair each db usage with the nearest watchdog dep that FOLLOWS it
    # AND comes before the next db usage (so a single watchdog can't
    # accidentally satisfy two handlers).
    db_sorted = sorted(db_positions)
    for i, db_idx in enumerate(db_sorted):
        next_db = db_sorted[i + 1] if i + 1 < len(db_sorted) else len(src)
        candidate_wds = [w for w in wd_positions if db_idx < w < next_db]
        assert candidate_wds, (
            f"{path}: handler at offset {db_idx} has no following "
            f"watchdog dep in its signature (db must precede watchdog "
            f"per v5.21.14)."
        )


def _all_positions(haystack: str, needle: str) -> list[int]:
    out: list[int] = []
    i = 0
    while True:
        j = haystack.find(needle, i)
        if j < 0:
            return out
        out.append(j)
        i = j + len(needle)
