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


def test_pool_size_bounded():
    """Total connection cap (pool_size + max_overflow) must stay small.
    Each connection is an OS thread; 150 was the leak amplifier. 50 is the
    historically-proven-workable ceiling now that tables are pruned."""
    pool_size = _int_kwarg("pool_size")
    max_overflow = _int_kwarg("max_overflow")
    assert pool_size <= 40, f"pool_size={pool_size} too large (each conn = 1 thread)"
    assert max_overflow <= 10, (
        f"max_overflow={max_overflow} too large — overflow is the only churn "
        f"path left; keep it tiny to bound the residual leak rate."
    )
    assert pool_size + max_overflow <= 60, (
        f"total pool cap {pool_size + max_overflow} > 60. Do NOT bump the pool "
        f"to fix exhaustion — that re-amplifies the aiosqlite thread leak. Fix "
        f"the real cause (holding a DB session across the upstream call)."
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
