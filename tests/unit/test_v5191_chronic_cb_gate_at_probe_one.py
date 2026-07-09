"""v5.19.1 — chronic-CB gate moved from sweep loop into ``_probe_one``.

Trigger: 2026-07-03 log sweep found Grok-Web-Devin firing every ~65 min
despite v5.17.1's chronic-CB gate. Diagnosis: v5.17.1 gated only the
``keepalive._probe_all_once`` sweep loop, but
``circuit_breaker._auto_probe`` also calls ``keepalive._probe_one``
directly (v5.3.9 auto-probe on CB half-open transition) and bypassed
the gate. Fix: relocate the gate check to ``_probe_one`` itself so
BOTH entry points honor it.
"""
from __future__ import annotations
from pathlib import Path


def test_gate_moved_into_probe_one():
    src = Path("app/monitoring/keepalive.py").read_text()
    # The gate code must appear inside the _probe_one function body,
    # BEFORE the model-resolution block. We check by the marker log line.
    assert "keepalive.chronic_cb_gated" in src
    assert "(v5.19.1 — at _probe_one)" in src


def test_sweep_side_gate_removed():
    """The sweep-side gate must be gone — its logic now lives in
    _probe_one. Keeping both = double-decrement of the backoff dict on
    sweep-side calls, which was never the intent."""
    src = Path("app/monitoring/keepalive.py").read_text()
    # The v5.17.1 sweep-side log-line format is gone.
    assert '"backoff_sec=%d (v5.17.1)"' not in src


def test_auto_probe_call_site_still_present():
    """The auto-probe call site in circuit_breaker.py must still exist —
    we didn't remove it, we just made it gate-aware transitively via
    the _probe_one gate."""
    src = Path("app/routing/circuit_breaker.py").read_text()
    assert "from app.monitoring.keepalive import _probe_one" in src
    assert "await _probe_one(provider)" in src


def test_get_consecutive_opens_imported_in_probe_one():
    """Gate reads consecutive_opens via the CB helper."""
    src = Path("app/monitoring/keepalive.py").read_text()
    assert "from app.routing.circuit_breaker import get_consecutive_opens" in src


def test_backoff_state_dict_still_present():
    """Module-level backoff dict still exists — it's now written from
    inside _probe_one instead of the sweep loop."""
    src = Path("app/monitoring/keepalive.py").read_text()
    assert "_chronic_backoff_until: dict[str, float] = {}" in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 19, 1), (
        f"expected >= 5.19.1, got {major}.{minor}.{patch}"
    )
