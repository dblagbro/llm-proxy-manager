"""v3.10.2 (ARCH-A) — DB connection-pool leak diagnostics.

The latent pool leak (www01 + GCP saturated QueuePool 13-20h
post-deploy → /health 500s) has an unknown root cause. v3.10.2 ships
the diagnostic toolkit:

  - an env-gated pool checkout tracer (``db_pool_trace``) that records
    the acquisition stack of every pool checkout, so a connection that
    never checks back in keeps an entry whose stack names the leak;
  - ``GET /cluster/db-pool-trace`` exposing those stacks (admin);
  - a trace summary folded into ``/health.dbPool``;
  - ``scripts/archa_pool_leak_harness.py`` — a load harness that
    isolates which request path leaks.
"""
from __future__ import annotations

import time
from pathlib import Path


# ── tracer registry + accessor ─────────────────────────────────────


def test_get_pool_checkout_trace_empty():
    from app.models.database import get_pool_checkout_trace, _pool_checkouts
    _pool_checkouts.clear()
    assert get_pool_checkout_trace() == []


def test_get_pool_checkout_trace_sorted_oldest_first():
    """The oldest checkout — the suspected leak — must sort first."""
    from app.models.database import get_pool_checkout_trace, _pool_checkouts
    _pool_checkouts.clear()
    now = time.monotonic()
    _pool_checkouts[1] = {"since": now - 5.0, "stack": "recent-frame"}
    _pool_checkouts[2] = {"since": now - 900.0, "stack": "OLD-LEAK-frame"}
    _pool_checkouts[3] = {"since": now - 60.0, "stack": "mid-frame"}
    try:
        trace = get_pool_checkout_trace()
        assert [e["stack"] for e in trace] == [
            "OLD-LEAK-frame", "mid-frame", "recent-frame",
        ]
        assert trace[0]["age_sec"] >= 800.0
        assert all("age_sec" in e and "stack" in e for e in trace)
    finally:
        _pool_checkouts.clear()


# ── config flag ────────────────────────────────────────────────────


def test_db_pool_trace_setting_exists_and_defaults_off():
    from app.config import settings
    assert hasattr(settings, "db_pool_trace")
    # Default OFF — traceback capture per checkout has overhead.
    assert settings.db_pool_trace is False


# ── wiring (source-level) ──────────────────────────────────────────


def test_tracer_listeners_gated_on_setting():
    src = Path("app/models/database.py").read_text()
    assert "if settings.db_pool_trace:" in src
    assert 'listens_for(engine.sync_engine, "checkout")' in src
    assert 'listens_for(engine.sync_engine, "checkin")' in src
    # checkin must remove the entry — otherwise every checkout "leaks"
    assert "_pool_checkouts.pop(" in src


def test_pool_trace_admin_endpoint_registered():
    src = Path("app/api/cluster.py").read_text()
    assert '"/cluster/db-pool-trace"' in src
    assert "get_pool_checkout_trace" in src
    assert "require_admin" in src


def test_health_snapshot_includes_trace_summary():
    """When tracing is on, /health.dbPool must carry the count + oldest
    age so the harness can read it without admin auth."""
    src = Path("app/api/cluster.py").read_text()
    assert "traced_checked_out" in src
    assert "oldest_checkout_age_sec" in src


# ── harness script ─────────────────────────────────────────────────


def test_harness_script_exists_and_compiles():
    import py_compile
    p = Path("scripts/archa_pool_leak_harness.py")
    assert p.exists(), "harness script missing"
    py_compile.compile(str(p), doraise=True)


def test_harness_isolates_three_request_paths():
    """The harness must test the three standing hypotheses separately —
    a single mixed run can't tell which path leaks."""
    src = Path("scripts/archa_pool_leak_harness.py").read_text()
    for phase in ("nonstream", "stream_consumed", "stream_abandoned"):
        assert phase in src, f"harness missing phase {phase}"
    # The streaming-abandoned phase must actually drop the connection
    # mid-stream (the disconnect-cleanup hypothesis).
    assert "break" in src
    # Pool state is read from /health (the uvicorn process's pool),
    # not a fresh in-process engine.
    assert "/health" in src and "checked_out" in src


def test_health_cache_hit_path_includes_dbpool():
    """v3.10.3 — the /health cache-hit branch must re-add dbPool. It
    previously re-added only circuitBreakers, so dbPool was absent on
    every cache hit (~2 of every 3s window) — which the ARCH-A harness
    polls and depends on."""
    src = Path("app/api/cluster.py").read_text()
    idx = src.index('_HEALTH_CACHE["body"] is not None')
    block = src[idx:idx + 1000]
    assert '"dbPool": _db_pool_snapshot()' in block
    assert '"circuitBreakers": get_all_states()' in block
