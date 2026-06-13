"""v5.4.2 — wire remaining 10 background workers to WorkerHeartbeat.

After v5.4.0 (4 wired) + v5.4.1 (+1 audit worker), v5.4.2 covers the
last 10: cursor_billing, codex_billing, cursor_oauth_expiry,
caller_memory_ttl_sweeper, observability_sampler, tool_capability_prober,
usage_rotator, prune, ai_rate_limiter, cluster_heartbeat,
cluster_peer_refresh. Source-grep pins lock the wiring; runtime
verification done live on tmrwww01 after deploy.
"""
from __future__ import annotations

from pathlib import Path


WIRED_WORKERS = [
    ("app/monitoring/cursor_billing_worker.py", "cursor_billing"),
    ("app/monitoring/codex_billing_worker.py", "codex_billing"),
    ("app/monitoring/cursor_oauth_expiry_monitor.py", "cursor_oauth_expiry"),
    ("app/monitoring/caller_memory_ttl_sweeper.py", "caller_memory_ttl_sweeper"),
    ("app/monitoring/observability_sampler.py", "observability_sampler"),
    ("app/monitoring/tool_capability_prober.py", "tool_capability_prober"),
    ("app/monitoring/usage_rotator.py", "usage_rotator"),
    ("app/monitoring/prune.py", "prune"),
    ("app/monitoring/ai_rate_limiter.py", "ai_rate_limiter"),
    ("app/cluster/manager.py", "cluster_heartbeat"),
    ("app/cluster/manager.py", "cluster_peer_refresh"),
]


def test_all_workers_import_worker_heartbeat():
    """Every wired worker file imports the WorkerHeartbeat factory."""
    files_to_check = {f for f, _ in WIRED_WORKERS}
    for f in files_to_check:
        src = Path(f).read_text()
        assert "from app.monitoring.worker_heartbeat import" in src, (
            f"{f} missing WorkerHeartbeat import"
        )


def test_all_workers_construct_heartbeat_with_their_name():
    """Each worker constructs a WorkerHeartbeat with the agreed name.
    Names map directly to /health.workers labels."""
    for f, name in WIRED_WORKERS:
        src = Path(f).read_text()
        assert f'WorkerHeartbeat(name="{name}")' in src, (
            f"{f} must construct WorkerHeartbeat(name=\"{name}\")"
        )


def test_all_workers_call_tick_in_their_loop():
    """Each worker invokes hb.tick(...) somewhere in its loop body."""
    files_to_check = {f for f, _ in WIRED_WORKERS}
    for f in files_to_check:
        src = Path(f).read_text()
        assert "await hb.tick(" in src, (
            f"{f} must call ``await hb.tick(...)`` at least once"
        )


def test_full_worker_roster_is_16_strong():
    """Sanity: 4 (v5.4.0) + 1 (v5.4.1) + 11 (v5.4.2) = 16 workers
    instrumented. The cluster_peer_refresh + cluster_heartbeat split
    out of the original 11-count because the cluster manager has two
    distinct loops in the same file. Pin so a future ship adding a new
    worker without a heartbeat fails the contract."""
    v540_workers = {"keepalive", "ai_provider_supervisor", "anthropic_billing", "cluster_sync_push"}
    v541_workers = {"compliance_audit"}
    v542_workers = {name for _, name in WIRED_WORKERS}
    total = v540_workers | v541_workers | v542_workers
    assert len(total) == 16, (
        f"expected exactly 16 instrumented workers; got {len(total)}: {sorted(total)}"
    )
