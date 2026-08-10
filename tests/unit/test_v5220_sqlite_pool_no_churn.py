"""v5.22.0 — regression guard for the aiosqlite connection-thread leak fix.

ROOT CAUSE (verified 2026-08-07 via py-spy): aiosqlite runs one OS thread
per DB connection. Pool CHURN (``pool_recycle`` + overflow create/destroy)
orphaned those threads on failed teardown — a 7-day node reached 232 threads
(~190 leaked), starving the asyncio event loop until ``/health`` (a no-op)
took 4-10s and the container went ``unhealthy``.

The fix is a small, NON-churning pool (``app/models/database.py``). This test
pins that config so the historical "bump the pool to fix exhaustion" reflex
(15→50→150) — which AMPLIFIED the leak — cannot silently return.

Static source assertions (hermetic, no engine/DB import) in the repo's
existing [WIRE]/[VER] guard style.
"""
import re
from pathlib import Path

_DB_SRC = (
    Path(__file__).resolve().parents[2] / "app" / "models" / "database.py"
).read_text()


def _int_kwarg(name: str) -> int:
    # Match the ACTUAL kwarg line (indented code), never a mention inside a
    # ``#`` comment — anchor to line-start + whitespace, which comment lines
    # (``    # ... pool_recycle=1800 ...``) never satisfy for the kwarg name.
    m = re.search(rf"^\s*{name}\s*=\s*(-?\d+)", _DB_SRC, re.MULTILINE)
    assert m, f"{name}= not found as a kwarg in database.py"
    return int(m.group(1))


def test_pool_recycle_disabled():
    """pool_recycle MUST be -1 (disabled). Recycling in-process SQLite
    connections is pure churn — the dominant thread-leak source. It was
    1800 (every 30 min) and that was the amplifier."""
    assert _int_kwarg("pool_recycle") == -1, (
        "pool_recycle must be -1 (disabled). Re-enabling it churns aiosqlite "
        "connections and re-opens the OS-thread leak. See v5.22.0."
    )


def test_self_heal_disabled_by_default():
    """The pool_leak_watcher self-heal ``engine.dispose()`` MUST default off.
    It disposed the pool while connections were checked out, orphaning them
    and leaking their aiosqlite threads (36 disposes in <1h → 362 threads).
    v5.22.0's mistake was leaving it on with a small pool that saturated
    constantly. Re-enabling it needs aiosqlite-thread-safe teardown first."""
    src = (
        Path(__file__).resolve().parents[2]
        / "app" / "monitoring" / "pool_leak_watcher.py"
    ).read_text()
    m = re.search(r'POOL_SELF_HEAL_ENABLED",\s*"([^"]*)"', src)
    assert m, "POOL_SELF_HEAL_ENABLED default not found"
    assert m.group(1) in ("0", "false", "no", "off"), (
        f"self-heal default is '{m.group(1)}' — must be OFF. engine.dispose() "
        f"orphans checked-out aiosqlite connections and leaks their threads."
    )


def test_pool_cap_bounded():
    """The pool cap = max concurrent SQLite accessors AND (with recycle=-1)
    the resting thread ceiling after a burst. v5.22.4 fixed the real cause of
    the pool pressure — the connection-hold leak (sessions pinned a connection
    across the upstream call/stream). With holds now short, the cap does NOT
    need to be tiny; it is sized to absorb bursts while keeping the resting
    thread count in the healthy band. It must still stay bounded — a runaway
    value re-introduces event-loop thread congestion (each aiosqlite conn is an
    OS thread). The cure for genuine exhaustion is the release-boundary /
    dispatch-re-select commits (v5.22.4), not an ever-larger pool."""
    cap = _int_kwarg("pool_size") + _int_kwarg("max_overflow")
    assert cap <= 60, (
        f"pool cap {cap} > 60 — too many resting aiosqlite threads risks "
        f"event-loop congestion. If exhaustion recurs, verify the v5.22.4 "
        f"connection-release commits, don't just raise the cap."
    )


def test_thread_leak_detection_wired():
    """The thread-count detection control (the metric that was missing when
    a node silently reached 232 threads) must remain wired in the watcher."""
    src = (
        Path(__file__).resolve().parents[2]
        / "app" / "monitoring" / "pool_leak_watcher.py"
    ).read_text()
    assert "THREAD_LEAK_CRITICAL" in src and "_thread_count" in src, (
        "pool_leak_watcher must monitor OS-thread count (the true aiosqlite "
        "leak signal), not just pool-slot utilization."
    )
