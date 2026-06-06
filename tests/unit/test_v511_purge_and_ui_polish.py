"""v5.1.1 — C2 time-range purge + B1/B2 UI follow-up pins."""
from __future__ import annotations

from pathlib import Path

import pytest


# ── C2 backend ─────────────────────────────────────────────────────


def test_purge_module_exists_with_both_endpoints():
    src = Path("app/api/admin_activity_purge.py").read_text()
    assert '@router.post("/api/admin/activity-log/purge")' in src
    assert '@router.post("/cluster/activity-log/purge")' in src


def test_purge_uses_hmac_for_peer_endpoint():
    src = Path("app/api/admin_activity_purge.py").read_text()
    assert "verify_cluster_request" in src
    assert "auth_headers_for" in src


def test_purge_writes_audit_to_compliance_policy_changes():
    src = Path("app/api/admin_activity_purge.py").read_text()
    assert "CompliancePolicyChange" in src
    assert 'scope="system"' in src


def test_purge_window_capped_at_90_days():
    """Single-call window cap matches api_key tombstone retention so a
    misclick can't wipe years of logs."""
    src = Path("app/api/admin_activity_purge.py").read_text()
    assert "_MAX_WINDOW_DAYS = 90" in src


def test_purge_router_registered():
    src = Path("app/main.py").read_text()
    assert (
        "from app.api.admin_activity_purge import router as admin_activity_purge_router"
        in src
    )
    assert "app.include_router(admin_activity_purge_router)" in src


def test_peer_endpoint_does_not_re_fanout():
    """Critical loop-prevention — the peer-side handler must NEVER
    call _fan_out_to_peers (would cycle endlessly)."""
    src = Path("app/api/admin_activity_purge.py").read_text()
    peer_idx = src.find("async def peer_purge_activity_log")
    assert peer_idx != -1
    next_def = src.find("async def ", peer_idx + 1)
    body = src[peer_idx:next_def if next_def != -1 else peer_idx + 3000]
    assert "_fan_out_to_peers" not in body, (
        "BUG: peer-side purge handler called _fan_out_to_peers — "
        "this would create an infinite cycle on the cluster."
    )


# ── B1 UI: deleted_at exposed by API ──────────────────────────────


def test_apikey_serialize_emits_deleted_at():
    src = Path("app/api/apikeys.py").read_text()
    assert '"deleted_at": utc_iso(k.deleted_at)' in src


# ── B2 backend (already pinned in v510 test; re-pin contract) ────


def test_create_key_copy_from_id_validates_source_exists():
    src = Path("app/api/apikeys.py").read_text()
    assert 'copy_from_id key {body.copy_from_id} not found' in src
