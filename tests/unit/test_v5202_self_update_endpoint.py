"""v5.20.2 — Self-update endpoint for integrating projects.

Operator's 2026-07-05 ask: instead of memo-through-operator, extend
the AI Integration Protocol so the caller's AI can update its own
key settings (bounded by pre-authorized permissions) AND propose new
protocols via a structured feedback channel.
"""
from __future__ import annotations
from pathlib import Path


def test_self_update_module_exists():
    p = Path("app/integration/self_update.py")
    assert p.exists()
    src = p.read_text()
    assert "async def integration_self_update" in src
    assert "class SelfUpdateRequest" in src
    assert "class SelfUpdateResult" in src


def test_self_edit_permissions_column_present():
    src = Path("app/models/db_apikey.py").read_text()
    assert "self_edit_permissions = Column(JSON" in src


def test_alter_table_for_self_edit_column():
    src = Path("app/models/database.py").read_text()
    assert "ADD COLUMN self_edit_permissions TEXT" in src


def test_router_mounted_in_integration_router():
    src = Path("app/api/integration.py").read_text()
    assert "from app.integration.self_update import router" in src
    assert "router.include_router(self_update_router)" in src


def test_forbidden_fields_include_privilege_escalation_guards():
    src = Path("app/integration/self_update.py").read_text()
    # self_edit_permissions itself must NEVER be self-editable
    assert '"self_edit_permissions"' in src
    # cost fields must NEVER be self-editable
    assert '"spending_cap_usd"' in src
    assert '"daily_hard_cap_usd"' in src
    # identity / status fields must NEVER be self-editable
    assert '"enabled"' in src
    assert '"key_type"' in src


def test_eligible_fields_include_refusal_and_mcp_toggles():
    """The whole point: caller can adjust refusal detection + MCP
    surface after negotiation without going through the operator."""
    src = Path("app/integration/self_update.py").read_text()
    assert "refusal_detection_enabled" in src
    assert "refusal_prompt_hardening" in src
    assert "mcp_tools_allow" in src


def test_protocol_proposal_channel_present():
    """The free-form 'here's what I need' channel — no state
    mutation, just an activity_log queue for operator review."""
    src = Path("app/integration/self_update.py").read_text()
    assert "protocol_proposal" in src
    assert "integration.protocol_proposal" in src


def test_compliance_and_oauth_prefixes_blocked():
    """Field name-guard blocks any compliance_/oauth_ column even if
    accidentally added to eligible list later."""
    src = Path("app/integration/self_update.py").read_text()
    assert 'startswith("compliance_")' in src
    assert 'startswith("oauth_")' in src


def test_null_permissions_disables_self_edit():
    """When self_edit_permissions is NULL, every attempted update
    lands in the 'denied' bucket. This is the safe default (opt-in)."""
    src = Path("app/integration/self_update.py").read_text()
    # The permissions list check: "not in this key's self_edit_permissions"
    assert "not in this key's self_edit_permissions" in src


def test_partial_apply_shape():
    """Response splits applied vs denied — caller sees exactly what
    the operator pre-authorized without a separate discovery call."""
    src = Path("app/integration/self_update.py").read_text()
    assert "applied: dict" in src
    assert "denied: dict" in src


def test_announce_documents_self_update():
    src = Path("app/integration/announce.py").read_text()
    assert "self_update" in src
    assert "/api/integration/self-update" in src
    assert "protocol_proposal_channel" in src


def test_announce_documents_refusal_detection_surface():
    """The v5.20.0 refusal-detection flags need to be discoverable via
    /announce so the caller's AI knows they exist."""
    src = Path("app/integration/announce.py").read_text()
    assert "refusal_detection" in src
    assert "X-Refusal-Detected" in src
    assert "task_substitution" in src


def test_lww_stamp_on_self_update():
    """Self-update MUST stamp last_user_edit_at so cluster sync
    LWW propagates the change to peers."""
    src = Path("app/integration/self_update.py").read_text()
    assert "last_user_edit_at" in src


def test_activity_log_always_written():
    """No-op self-updates still get audited — 'nothing applied' is
    still auditable so the operator can see attempted misuses."""
    src = Path("app/integration/self_update.py").read_text()
    assert "integration.self_update" in src
    assert "integration.self_update_noop" in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 20, 2), (
        f"expected >= 5.20.2, got {major}.{minor}.{patch}"
    )
