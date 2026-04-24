"""Unit tests for RBAC (Wave 6)."""
import sys
import types
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.auth.rbac import (
    get_role, list_roles, permissions_for, has_permission,
    ALL_PERMISSIONS,
    PROVIDERS_READ, PROVIDERS_WRITE, KEYS_READ, KEYS_WRITE, KEYS_REVEAL,
    USERS_READ, USERS_WRITE, SETTINGS_READ, SETTINGS_WRITE,
    AUDIT_READ, AUDIT_EXPORT,
)


class TestGetRole:
    def test_admin(self):
        r = get_role("admin")
        assert r.name == "admin"

    def test_operator(self):
        r = get_role("operator")
        assert r.name == "operator"

    def test_viewer(self):
        r = get_role("viewer")
        assert r.name == "viewer"

    def test_case_insensitive(self):
        assert get_role("ADMIN").name == "admin"
        assert get_role("Viewer").name == "viewer"

    def test_none_returns_none(self):
        assert get_role(None) is None
        assert get_role("") is None
        assert get_role("not-a-role") is None


class TestAdminPermissions:
    def test_admin_has_all_permissions(self):
        perms = permissions_for("admin")
        assert perms == ALL_PERMISSIONS

    def test_admin_can_reveal_keys(self):
        assert has_permission("admin", KEYS_REVEAL)

    def test_admin_can_manage_users(self):
        assert has_permission("admin", USERS_WRITE)

    def test_admin_can_export_audit(self):
        assert has_permission("admin", AUDIT_EXPORT)


class TestOperatorPermissions:
    def test_operator_can_write_providers(self):
        assert has_permission("operator", PROVIDERS_WRITE)

    def test_operator_can_write_keys(self):
        assert has_permission("operator", KEYS_WRITE)

    def test_operator_cannot_reveal_keys(self):
        assert not has_permission("operator", KEYS_REVEAL)

    def test_operator_cannot_manage_users(self):
        assert not has_permission("operator", USERS_WRITE)

    def test_operator_cannot_write_settings(self):
        assert not has_permission("operator", SETTINGS_WRITE)

    def test_operator_can_read_settings(self):
        assert has_permission("operator", SETTINGS_READ)


class TestViewerPermissions:
    def test_viewer_can_read_providers(self):
        assert has_permission("viewer", PROVIDERS_READ)

    def test_viewer_cannot_write_providers(self):
        assert not has_permission("viewer", PROVIDERS_WRITE)

    def test_viewer_cannot_write_keys(self):
        assert not has_permission("viewer", KEYS_WRITE)

    def test_viewer_cannot_reveal_keys(self):
        assert not has_permission("viewer", KEYS_REVEAL)


class TestUnknownRole:
    def test_none_role_has_no_permissions(self):
        assert permissions_for(None) == frozenset()

    def test_garbage_role_has_no_permissions(self):
        assert permissions_for("totally-fake") == frozenset()
        assert not has_permission("totally-fake", PROVIDERS_READ)


class TestListRoles:
    def test_returns_all_three_defaults(self):
        names = {r.name for r in list_roles()}
        assert names == {"admin", "operator", "viewer"}

    def test_roles_are_frozen(self):
        r = get_role("admin")
        with pytest.raises((AttributeError, Exception)):
            r.name = "hijacked"
