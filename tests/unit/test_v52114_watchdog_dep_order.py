"""v5.21.14 — disconnect-watchdog dep-order fix across ALL watchdog routes.

Root cause of the recurring DB-pool leak that v5.21.13 self-heal only
masked: the client-disconnect watchdog cancels the handler task on
disconnect. FastAPI cleans up ``yield`` dependencies LIFO, so whichever
is declared LAST is torn down FIRST. When ``_watchdog`` is declared
BEFORE ``db=Depends(get_db)``, get_db is cleaned up first — while the
watcher is still live — so a disconnect mid-``session.close()`` leaks the
pool slot.

v5.21.12 fixed exactly this for cluster.py by declaring ``db`` FIRST
(so LIFO closes get_db LAST, after the watchdog has stopped). But the
same anti-pattern remained in six hot-path routes: messages, completions,
audio, images, integration, responses. This pins the invariant EVERYWHERE:
in any route that uses BOTH deps, get_db must be declared before the
watchdog.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Every route module that wires the disconnect watchdog.
_WATCHDOG_ROUTES = [
    "app/api/messages.py",
    "app/api/completions.py",
    "app/api/audio.py",
    "app/api/images.py",
    "app/api/integration.py",
    "app/api/responses.py",
    "app/api/cluster.py",
]


def _line_indices(lines, needle):
    return [i for i, l in enumerate(lines) if needle in l]


@pytest.mark.parametrize("path", _WATCHDOG_ROUTES)
def test_get_db_declared_before_watchdog(path):
    """For each watchdog occurrence, a Depends(get_db) must be declared
    on an EARLIER line in the same signature (within a small window), so
    FastAPI's LIFO cleanup closes get_db last."""
    src = Path(path).read_text()
    lines = src.splitlines()
    db_lines = _line_indices(lines, "Depends(get_db)")
    wd_lines = _line_indices(lines, "Depends(watch_for_disconnect)")
    assert wd_lines, f"{path}: expected at least one watchdog dep"
    for w in wd_lines:
        earlier_db = [d for d in db_lines if d < w and (w - d) <= 8]
        assert earlier_db, (
            f"{path}: watch_for_disconnect at line {w+1} has no Depends(get_db) "
            f"declared before it in the same signature — LIFO cleanup would "
            f"close get_db while the watcher is still live and leak a pool slot"
        )


def test_no_watchdog_route_regresses_to_watchdog_first():
    """Global guard: no watchdog route may have the buggy adjacency
    ``_watchdog ...\\n    db: ... Depends(get_db)`` where watchdog is the
    line IMMEDIATELY above db."""
    bad = []
    for path in _WATCHDOG_ROUTES:
        lines = Path(path).read_text().splitlines()
        for i in range(len(lines) - 1):
            if ("Depends(watch_for_disconnect)" in lines[i]
                    and "Depends(get_db)" in lines[i + 1]):
                bad.append(f"{path}:{i+1}")
    assert not bad, f"watchdog-immediately-before-get_db (buggy order) at: {bad}"


def test_version_bumped():
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) >= (5, 21, 14)
