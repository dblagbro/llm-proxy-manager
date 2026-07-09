"""v5.15.0 Phase 1 (#508) — Per-account OAuth fan-out.

Phase 1 = schema + admin endpoints + seeder. NO dispatch change yet.
Phase 2 (v5.15.1) will flip dispatch + add frontend Accounts panel per
operator's 2026-06-30 sign-off.

This test file locks in the Phase 1 contract:
- ProviderOAuthAccount ORM model exists + is registered with Base
- Provider gains `oauth_account_strategy` column + `oauth_accounts` relationship
- oauth_account_seeder is idempotent + copies legacy tokens
- admin endpoints CRUD works (list, create, patch, delete)
- audit rows land on every write
- settings are exposed
- version bumped
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


# ── (1) ORM model surface ─────────────────────────────────────────────


def test_provider_oauth_account_importable_from_db_shim():
    from app.models.db import ProviderOAuthAccount
    from app.models.db_provider import ProviderOAuthAccount as _direct
    assert ProviderOAuthAccount is _direct


def test_provider_oauth_account_registered_with_base():
    from app.models.db_base import Base
    tables = set(Base.metadata.tables.keys())
    assert "provider_oauth_accounts" in tables


def test_provider_oauth_account_columns_present():
    from app.models.db import ProviderOAuthAccount
    for col in (
        "id", "provider_id", "label",
        "access_token", "refresh_token", "oauth_expires_at",
        "enabled", "last_used_at", "utilization_pct",
        "captured_via", "created_at", "updated_at",
        "deleted_at", "last_user_edit_at",
    ):
        assert hasattr(ProviderOAuthAccount, col), f"missing {col}"


def test_provider_gains_oauth_account_strategy_column():
    from app.models.db import Provider
    assert hasattr(Provider, "oauth_account_strategy")


def test_provider_has_oauth_accounts_relationship():
    from app.models.db import Provider
    assert hasattr(Provider, "oauth_accounts")


# ── (2) Migration ALTER for providers.oauth_account_strategy ─────────


def test_provider_column_alter_wired_in_migration():
    src = Path("app/models/database.py").read_text()
    assert "ALTER TABLE providers ADD COLUMN oauth_account_strategy TEXT" in src


# ── (3) Seeder ────────────────────────────────────────────────────────


def test_seeder_module_importable():
    from app.providers.oauth_account_seeder import (
        seed_missing_accounts, _OAUTH_PROVIDER_TYPES,
    )


def test_seeder_scope_covers_all_oauth_types():
    from app.providers.oauth_account_seeder import _OAUTH_PROVIDER_TYPES
    for t in ("cursor-oauth", "codex-oauth", "claude-oauth"):
        assert t in _OAUTH_PROVIDER_TYPES


def test_seeder_wired_in_init_db():
    """v5.15.0 boot MUST call the seeder so existing OAuth providers
    have a matching child row before v5.15.1 dispatch flip lands."""
    src = Path("app/models/database.py").read_text()
    assert "from app.providers.oauth_account_seeder import seed_missing_accounts" in src
    assert "await seed_missing_accounts(" in src


# ── (4) Admin router surface ─────────────────────────────────────────


def test_admin_router_module_importable():
    from app.api.admin_provider_oauth_accounts import router
    assert router.prefix == "/api/admin/providers/{provider_id}/oauth-accounts"


def test_admin_router_registered_in_main():
    src = Path("app/main.py").read_text()
    assert "admin_provider_oauth_accounts_router" in src
    assert "from app.api.admin_provider_oauth_accounts import router" in src


def test_admin_router_covers_the_five_endpoints():
    from app.api.admin_provider_oauth_accounts import router
    paths_and_methods = set()
    for r in router.routes:
        for m in (getattr(r, "methods", None) or []):
            paths_and_methods.add((m, r.path))
    # POST + GET on collection root
    assert ("GET", "/api/admin/providers/{provider_id}/oauth-accounts") in paths_and_methods
    assert ("POST", "/api/admin/providers/{provider_id}/oauth-accounts") in paths_and_methods
    # PATCH + DELETE + POST-probe on per-account path
    per_acc = "/api/admin/providers/{provider_id}/oauth-accounts/{account_id}"
    assert ("PATCH", per_acc) in paths_and_methods
    assert ("DELETE", per_acc) in paths_and_methods
    assert ("POST", per_acc + "/probe") in paths_and_methods


def test_admin_writes_audit_row_on_create():
    """Every mutation MUST write a compliance_policy_changes row.
    Static-grep catches accidental removal of the audit call."""
    src = Path("app/api/admin_provider_oauth_accounts.py").read_text()
    # Every mutating endpoint calls _write_audit
    assert src.count("_write_audit(") >= 3  # create, patch, delete
    assert "CompliancePolicyChange" in src
    # Correct shape — matches v5.1.2 pattern
    assert 'scope="per_provider"' in src
    assert 'target_id=provider_id' in src


# ── (5) Settings ─────────────────────────────────────────────────────


def test_settings_exposed():
    from app.config import settings
    assert hasattr(settings, "oauth_account_fanout_enabled")
    assert hasattr(settings, "oauth_account_default_strategy")
    assert settings.oauth_account_fanout_enabled is True
    assert settings.oauth_account_default_strategy == "least_utilized"


# ── (6) Version ──────────────────────────────────────────────────────


def test_version_bumped():
    """v5.15.0 shipped Phase 1; assert at-or-beyond so future bumps don't
    require touching this test."""
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', src)
    assert m, f"could not parse __version__ from {src!r}"
    major, minor = int(m.group(1)), int(m.group(2))
    assert (major, minor) >= (5, 15), f"expected >= 5.15, got {major}.{minor}"


# ── (7) Phase 1 explicitly does NOT change dispatch ─────────────────


def test_phase1_boundary_now_lifted_in_phase2():
    """Phase 1 shipped v5.15.0 (schema + endpoints, no dispatch). Phase 2
    ships v5.15.1 which wires ``apply_fanout_to_kwargs`` into
    messages.py `_call_with_route`. This test is a marker that the
    Phase 1 boundary is intentionally lifted from v5.15.1 onward."""
    src = Path("app/__version__.py").read_text()
    import re
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # v5.15.0 is the last version where the boundary applies.
    if (major, minor, patch) == (5, 15, 0):
        src2 = Path("app/api/messages.py").read_text()
        assert "apply_fanout_to_kwargs" not in src2, (
            "v5.15.0 must not wire dispatch — save for v5.15.1"
        )
