"""v5.4.0 — WorkerHeartbeat factory + /health surface (BUG-069 / BUG-074).

Tests the heartbeat module in isolation + verifies that the four wired
workers (keepalive, ai_provider_supervisor, anthropic_billing,
cluster_sync_push) call ``tick()`` in their loop body. Source-grep
tests are used because exercising the full loop (asyncio.sleep)
in-test is brittle and the wiring contract is the load-bearing
guarantee — once the call is in the source, the heartbeat happens.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ── Module exports ──────────────────────────────────────────────────


def test_worker_heartbeat_module_exposes_factory_and_snapshot():
    from app.monitoring.worker_heartbeat import (
        WorkerHeartbeat,
        snapshot_all,
        register_expected_interval,
    )
    assert callable(WorkerHeartbeat)
    assert callable(snapshot_all)
    assert callable(register_expected_interval)


def test_heartbeat_key_shape_is_stable():
    """system_settings keys MUST start with ``worker.<name>.<field>``
    so /health's snapshot scan finds them. Locking the prefix prevents
    a refactor from breaking the implicit contract."""
    from app.monitoring import worker_heartbeat as wh
    assert wh._key("keepalive", "last_run") == "worker.keepalive.last_run"
    assert wh._key("ai_provider_supervisor", "last_status") == "worker.ai_provider_supervisor.last_status"


def test_register_expected_interval_is_idempotent():
    from app.monitoring.worker_heartbeat import (
        register_expected_interval,
        _EXPECTED_INTERVALS,
    )
    register_expected_interval("test_w_idempotent", 100.0)
    register_expected_interval("test_w_idempotent", 100.0)
    assert _EXPECTED_INTERVALS["test_w_idempotent"] == 100.0
    register_expected_interval("test_w_idempotent", 200.0)
    assert _EXPECTED_INTERVALS["test_w_idempotent"] == 200.0


# ── Wiring contracts (source-grep) ──────────────────────────────────


def test_keepalive_loop_calls_worker_heartbeat_tick():
    src = Path("app/monitoring/keepalive.py").read_text()
    assert "from app.monitoring.worker_heartbeat import" in src
    assert "WorkerHeartbeat(name=\"keepalive\")" in src
    assert "await hb.tick(" in src


def test_ai_supervisor_loop_calls_worker_heartbeat_tick():
    src = Path("app/monitoring/ai_provider_supervisor.py").read_text()
    assert "from app.monitoring.worker_heartbeat import" in src
    assert "WorkerHeartbeat(name=\"ai_provider_supervisor\")" in src
    assert "await hb.tick(" in src


def test_anthropic_billing_loop_calls_worker_heartbeat_tick():
    src = Path("app/monitoring/anthropic_billing_worker.py").read_text()
    assert "from app.monitoring.worker_heartbeat import" in src
    assert "WorkerHeartbeat(name=\"anthropic_billing\")" in src


def test_cluster_sync_push_loop_calls_worker_heartbeat_tick():
    src = Path("app/cluster/manager.py").read_text()
    assert "from app.monitoring.worker_heartbeat import" in src
    assert "WorkerHeartbeat(name=\"cluster_sync_push\")" in src


# ── /health envelope surface ────────────────────────────────────────


def test_health_envelope_includes_workers_block_in_source():
    """Both code paths (cache-miss + cache-hit) must include the
    workers block. Pin prevents the v3.10.3 regression of dropping a
    field from the cache-hit branch."""
    src = Path("app/api/cluster.py").read_text()
    # cache-miss path
    assert '"workers": await _workers_snapshot()' in src
    # cache-hit path also writes workers (look for second occurrence)
    workers_count = src.count('"workers": await _workers_snapshot()')
    assert workers_count >= 2, (
        f"workers field must appear in both cache-miss AND cache-hit branches; "
        f"found {workers_count} occurrence(s)"
    )
    # excluded from the cached body so it stays live
    assert '"workers"' in src and 'k not in ("circuitBreakers", "dbPool", "workers")' in src


def test_health_envelope_workers_block_uses_snapshot_all():
    """The workers block goes through worker_heartbeat.snapshot_all()
    so the stale-detection logic kicks in. A direct DB query would
    skip the registered-interval check."""
    src = Path("app/api/cluster.py").read_text()
    assert "from app.monitoring.worker_heartbeat import snapshot_all" in src


# ── Diagnostic admin endpoint (BUG-070) ─────────────────────────────


def test_admin_ai_supervisor_router_exists():
    from app.api.admin_ai_supervisor import router
    assert router is not None
    # Single endpoint: POST /api/admin/ai-supervisor/run-once
    routes = [r for r in router.routes if hasattr(r, "path")]
    assert any("/run-once" in r.path for r in routes), (
        f"expected /run-once endpoint; got {[r.path for r in routes]}"
    )


def test_admin_ai_supervisor_router_registered_in_main():
    src = Path("app/main.py").read_text()
    assert "from app.api.admin_ai_supervisor import router as admin_ai_supervisor_router" in src
    assert "app.include_router(admin_ai_supervisor_router)" in src


@pytest.mark.asyncio
async def test_supervisor_run_once_endpoint_handles_crash():
    """If _scan_all_once raises, the endpoint must return ok=False
    with the error type, NOT propagate the crash to the HTTP layer.
    This is the bug-070 diagnostic value — capturing crashes that
    would otherwise be silent."""
    from app.api.admin_ai_supervisor import supervisor_run_once
    with patch(
        "app.monitoring.ai_provider_supervisor._scan_all_once",
        new=AsyncMock(side_effect=RuntimeError("simulated crash")),
    ):
        result = await supervisor_run_once(db=None, _admin=None)
    assert result["ok"] is False
    assert result["error_type"] == "RuntimeError"
    assert "simulated crash" in result["error"]


@pytest.mark.asyncio
async def test_supervisor_run_once_endpoint_returns_counts():
    """Happy path: _scan_all_once returns the count dict; endpoint
    surfaces it verbatim plus the friendly top-level keys."""
    from app.api.admin_ai_supervisor import supervisor_run_once
    fake_counts = {"reviewed": 3, "skipped_locked": 1, "skipped_no_traffic": 2}
    with patch(
        "app.monitoring.ai_provider_supervisor._scan_all_once",
        new=AsyncMock(return_value=fake_counts),
    ):
        result = await supervisor_run_once(db=None, _admin=None)
    assert result["ok"] is True
    assert result["counts"] == fake_counts
    assert result["reviewed"] == 3
    assert result["skipped_locked"] == 1
    assert result["skipped_no_traffic"] == 2
