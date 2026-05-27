"""v3.9.8 — pool diagnostics in /health (P3 defense-in-depth).

After the 2026-05-14 www01 pool exhaustion, the only way to read
SQLAlchemy QueuePool state was via ``sudo docker exec`` inside the
container — surfacing it on /health closes that gap so operators can
diagnose live pool state from the canonical URL.
"""
from __future__ import annotations

from pathlib import Path


def test_health_endpoint_emits_dbPool_field():
    src = Path("app/api/cluster.py").read_text()
    assert '"dbPool"' in src
    assert "_db_pool_snapshot()" in src


def test_dbPool_snapshot_function_pulls_from_engine():
    src = Path("app/api/cluster.py").read_text()
    assert "def _db_pool_snapshot" in src
    assert "from app.models.database import engine" in src
    assert "pool.checkedout" in src
    assert "pool.overflow" in src
    assert "pool.size" in src


def test_dbPool_snapshot_never_raises():
    """Pool introspection must be wrapped in try/except so health
    endpoint never 500s on a pool query error."""
    src = Path("app/api/cluster.py").read_text()
    idx = src.find("def _db_pool_snapshot")
    # v4.4.22 — extract the full function body, not a fixed 1500-char
    # slice. The prior slice was outgrown by the v4.4.22 async-tracer
    # surfacing added inside the try block, pushing the matching
    # ``except Exception`` past the window and breaking this test.
    end = src.find("\n\n\n", idx)
    if end == -1:
        end = idx + 4000
    fn = src[idx:end]
    assert "try:" in fn
    assert "except Exception" in fn


def test_health_cache_excludes_dbPool():
    """Pool state must be live (re-read on every /health call) — not
    cached for 3s like the static fields. Cluster.py already excludes
    circuitBreakers from the cache; dbPool joins that exclusion."""
    src = Path("app/api/cluster.py").read_text()
    # The cache excludes dbPool
    assert 'k not in ("circuitBreakers", "dbPool")' in src


def test_snapshot_function_runs_against_live_engine():
    """Smoke check: the helper produces a dict when called against
    the real configured engine."""
    from app.api.cluster import _db_pool_snapshot
    snap = _db_pool_snapshot()
    assert isinstance(snap, dict)
    # Either the function returned a real snapshot OR an error key —
    # both are acceptable. Live engine is configured at import time.
    assert any(k in snap for k in ("size", "error"))
