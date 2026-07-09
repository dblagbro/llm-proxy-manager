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
``test_v5717_disconnect_watchdog.py``: the watchdog dep MUST appear
before ``db = Depends(get_db)`` so the watcher is armed before a
session is checked out.
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
    """Every handler that takes ``db = Depends(get_db)`` MUST also
    take ``_watchdog = Depends(watch_for_disconnect)`` listed BEFORE it.

    We assert per-handler ordering by walking the file and pairing each
    ``Depends(get_db)`` with the nearest preceding
    ``Depends(watch_for_disconnect)`` — that pairing must exist and be
    closer than the previous handler's pairing.
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

    # Pair each db usage with the nearest watchdog dep that precedes it
    # AND comes after the previous db usage (so a single watchdog can't
    # accidentally satisfy two handlers).
    prev_db = -1
    for db_idx in db_positions:
        candidate_wds = [w for w in wd_positions if prev_db < w < db_idx]
        assert candidate_wds, (
            f"{path}: handler at offset {db_idx} has no preceding "
            f"watchdog dep in its signature."
        )
        prev_db = db_idx


def _all_positions(haystack: str, needle: str) -> list[int]:
    out: list[int] = []
    i = 0
    while True:
        j = haystack.find(needle, i)
        if j < 0:
            return out
        out.append(j)
        i = j + len(needle)
