"""v5.21.13 — pool_leak_watcher self-heal.

The 2026-07-23 login outage: a residual connection-pool leak filled the
pool (size 50 + overflow 100 = 150); every DB-backed request, including
``/api/auth/login``, then 500'd until an operator manually restarted the
container. Pre-v5.21.13 the watcher only DUMPED a trace and never acted,
and its own DB-backed heartbeat also failed under saturation.

This pins the fix: the watcher recycles the pool in-process
(``engine.dispose()``) on SUSTAINED saturation, using DB-free pool
stats, so a leak degrades to a periodic self-recycle instead of a
user-visible outage — no manual restart required.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app.monitoring.pool_leak_watcher as plw


# ── Static pins ──────────────────────────────────────────────────────

def test_self_heal_calls_engine_dispose():
    """Remediation MUST be engine.dispose() — the DB-free, no-restart
    pool recycle. Anything requiring a connection would fail under the
    very saturation it's meant to cure."""
    src = Path("app/monitoring/pool_leak_watcher.py").read_text()
    assert "engine.dispose()" in src
    assert "async def _self_heal_recycle" in src


def test_heal_is_gated_on_sustained_saturation():
    """A single high sample must NOT trigger a recycle — only a streak
    does, so a legitimate burst of concurrent streams that drains on its
    own is never mistaken for a leak."""
    src = Path("app/monitoring/pool_leak_watcher.py").read_text()
    assert "_HEAL_SUSTAINED_POLLS" in src
    assert "_consecutive_high" in src
    assert "_HEAL_COOLDOWN_SEC" in src
    assert plw._HEAL_SUSTAINED_POLLS >= 2


def test_detection_and_heal_are_db_free():
    """Utilization is read from in-memory pool counters, not a query —
    that's what lets detection/heal run when the pool is fully starved.
    The heal path must run BEFORE the DB-backed heartbeat.tick()."""
    src = Path("app/monitoring/pool_leak_watcher.py").read_text()
    heal_pos = src.find("_self_heal_recycle(util)")
    tick_pos = src.find("await heartbeat.tick(")
    assert 0 < heal_pos < tick_pos, "self-heal must run before heartbeat.tick()"


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) >= (5, 21, 13)


# ── Behavioural ──────────────────────────────────────────────────────

def test_recycle_invokes_dispose(monkeypatch=None):
    """_self_heal_recycle awaits engine.dispose() and reports success."""
    async def _run():
        fake_engine = type("E", (), {})()
        fake_engine.pool = type(
            "P", (), {"size": lambda s: 50, "checkedout": lambda s: 150,
                      "overflow": lambda s: 100},
        )()
        fake_engine.dispose = AsyncMock()
        with patch.dict("sys.modules"):
            import app.models.database as dbmod
            with patch.object(dbmod, "engine", fake_engine, create=True):
                ok = await plw._self_heal_recycle(0.99)
        fake_engine.dispose.assert_awaited_once()
        assert ok is True

    asyncio.run(_run())


def test_recycle_failure_is_swallowed():
    """A dispose() that raises must NOT propagate — a failed heal can't
    be allowed to kill the watcher loop."""
    async def _run():
        fake_engine = type("E", (), {})()
        fake_engine.pool = type(
            "P", (), {"size": lambda s: 50, "checkedout": lambda s: 150,
                      "overflow": lambda s: 100},
        )()
        fake_engine.dispose = AsyncMock(side_effect=RuntimeError("boom"))
        import app.models.database as dbmod
        with patch.object(dbmod, "engine", fake_engine, create=True):
            ok = await plw._self_heal_recycle(0.99)
        assert ok is False

    asyncio.run(_run())
