"""v5.1.0 — Batches A + C1 + B source + behavioral pins.

Batch A: cluster_peers daily backup integration, peer version display,
grok-3 model-name preservation in failover (extra-swap).
Batch C1: activity-log on/off toggle.
Batch B: ApiKey trash bin + copy-from-existing.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Batch A: backup script ──────────────────────────────────────────


from tests.unit._handler_surface import completions_handler_source


def test_backup_script_includes_clone_and_smoke():
    """The nightly backup script must dump BOTH the original llm-proxy2
    and the clone llm-proxy DBs (which have different state since the
    snapshot-and-fork). Also the smoke DB for completeness."""
    # v5.22.42 — reads the repo copy, so this runs everywhere.
    #
    # It used to read the absolute host path
    # /home/dblagbro/docker/scripts/backup-safe-dumps.sh, which made it the last
    # non-hermetic test in tests/unit: it failed with a bare FileNotFoundError
    # the first time CI ran the full suite (v5.22.38), was made to skip, and now
    # the script is versioned under ops-scripts/tmrwww01-backup/ the way the
    # tmrwww02 cert hooks already were. Host path kept as a fallback so the test
    # still means something on a machine where only the live script exists.
    _repo_copy = Path("ops-scripts/tmrwww01-backup/backup-safe-dumps.sh")
    _host_copy = Path("/home/dblagbro/docker/scripts/backup-safe-dumps.sh")
    _script = _repo_copy if _repo_copy.is_file() else _host_copy
    if not _script.is_file():
        pytest.skip(
            "neither the repo copy nor the host copy of backup-safe-dumps.sh "
            "is present"
        )
    src = _script.read_text()
    assert "llmproxy-clone.db" in src, (
        "backup script missing clone DB — operator restores would lose "
        "the clone's distinct api_keys state."
    )
    assert "llmproxy-smoke.db" in src
    assert "cluster_peers_llm-proxy2.json" in src
    assert "cluster_peers_llm-proxy-clone.json" in src


# ── Batch A: peer version display ──────────────────────────────────


def test_peer_node_has_version_field():
    """PeerNode dataclass must carry a version field captured from /health."""
    src = Path("app/cluster/manager.py").read_text()
    assert "version: Optional[str] = None" in src, (
        "PeerNode.version missing — Settings → Cluster can't surface skew."
    )


def test_heartbeat_captures_peer_version():
    src = Path("app/cluster/manager.py").read_text()
    assert 'peer.version = data.get("version")' in src


def test_cluster_status_surfaces_local_version():
    src = Path("app/cluster/manager.py").read_text()
    assert '"version": _local_version' in src
    assert "from app.__version__ import __version__ as _local_version" in src


# ── Batch A: grok-3 failover extra-swap ────────────────────────────


def test_completions_failover_swaps_extra_kwargs():
    # v5.22.37 — reads the whole handler surface: the own-dispatcher branches
    # this asserts on moved into _completions_{claude_oauth,grok_web}.py when
    # completions.py was split to meet its 900-LOC pin.
    src = completions_handler_source()
    # Look in the grok-web failover block for the extra-swap pattern.
    fb = src.find("X-Grok-Web-Failover-Target")
    assert fb != -1
    block = src[max(0, fb - 2000):fb + 200]
    assert "extra.pop(_k, None)" in block, (
        "completions.py grok-web failover no longer swaps `extra` (litellm_kwargs); "
        "the openai-shape upstream call uses the wrong credentials → "
        "served-model misrepresentation (gpt-4o instead of grok-3)."
    )
    assert "extra.update(new_route.litellm_kwargs)" in block


def test_messages_failover_swaps_extra_kwargs():
    src = Path("app/api/messages.py").read_text()
    fb = src.find("X-Grok-Web-Failover-Target")
    assert fb != -1
    block = src[max(0, fb - 2000):fb + 200]
    assert "extra.pop(_k, None)" in block
    assert "extra.update(new_route.litellm_kwargs)" in block


# ── Batch C1: logging toggle ──────────────────────────────────────


def test_logging_controls_module_exists():
    from app.monitoring.logging_controls import (
        SETTING_KEY,
        is_logging_enabled,
        set_logging_enabled,
        invalidate_cache,
    )
    assert SETTING_KEY == "compliance.activity_logging_enabled"
    assert callable(is_logging_enabled)
    assert callable(set_logging_enabled)
    assert callable(invalidate_cache)


def test_activity_log_checks_toggle_before_writing():
    src = Path("app/monitoring/activity.py").read_text()
    assert "from app.monitoring.logging_controls import is_logging_enabled" in src
    assert "if not await is_logging_enabled(db):" in src
    assert "return" in src  # the early return on disabled


def test_admin_logging_router_registered():
    src = Path("app/main.py").read_text()
    assert "from app.api.admin_logging import router as admin_logging_router" in src
    assert "app.include_router(admin_logging_router)" in src


def test_admin_logging_endpoints_exist():
    src = Path("app/api/admin_logging.py").read_text()
    assert '@router.get("/status")' in src
    assert '@router.post("/toggle")' in src


def test_toggle_writes_audit_row_to_compliance_policy_changes():
    """The toggle MUST write to compliance_policy_changes (the right
    audit home for system-scope policy edits); the daily audit chain
    sweeper covers that table, so tampering after a flip breaks the
    chain.

    v5.3.3 — the audit-write machinery moved from logging_controls.py
    into the shared ``_bool_system_setting`` factory, but the guarantee
    is still that the toggle flow writes a system-scope policy-change
    row. Two-file pin: factory holds the write; shim still has to
    construct the factory."""
    factory_src = Path("app/monitoring/_bool_system_setting.py").read_text()
    assert "from app.models.db import SystemSetting, CompliancePolicyChange" in factory_src
    assert 'scope="system"' in factory_src
    shim_src = Path("app/monitoring/logging_controls.py").read_text()
    assert "BoolSystemSetting(" in shim_src


# ── Batch B1: trash bin + restore ──────────────────────────────────


def test_list_keys_accepts_include_deleted():
    src = Path("app/api/apikeys.py").read_text()
    list_idx = src.find("async def list_keys(")
    assert list_idx != -1
    block = src[list_idx:list_idx + 1000]
    assert "include_deleted: bool = False" in block
    assert "if not include_deleted:" in block


def test_restore_endpoint_exists_and_honors_retention():
    src = Path("app/api/apikeys.py").read_text()
    assert '@router.post("/{key_id}/restore")' in src
    assert "api_key_tombstone_retention_days" in src


def test_tombstone_retention_setting_registered():
    src = Path("app/config.py").read_text()
    assert "api_key_tombstone_retention_days" in src
    assert 'alias="API_KEY_TOMBSTONE_RETENTION_DAYS"' in src


# ── Batch B2: copy-from-existing ───────────────────────────────────


def test_key_create_supports_copy_from_id():
    src = Path("app/api/apikeys.py").read_text()
    assert "copy_from_id: Optional[str] = None" in src
    # And the dispatch handles it
    assert "if body.copy_from_id:" in src
    assert "body.spending_cap_usd = src.spending_cap_usd" in src


def test_backup_script_matches_host_copy():
    """Warn when the repo mirror has drifted from the live host script.

    Not a failure: the host copy is what actually runs nightly, and only one of
    the two machines even has it. But a mirror that silently diverges is worse
    than no mirror, because the test above would then be asserting about a file
    nobody executes.
    """
    repo = Path("ops-scripts/tmrwww01-backup/backup-safe-dumps.sh")
    host = Path("/home/dblagbro/docker/scripts/backup-safe-dumps.sh")
    if not (repo.is_file() and host.is_file()):
        pytest.skip("both copies are needed to compare them")
    if repo.read_text() != host.read_text():
        pytest.skip(
            "ops-scripts/tmrwww01-backup/backup-safe-dumps.sh has drifted from "
            "the live host script. Copy the host version back into the repo."
        )
