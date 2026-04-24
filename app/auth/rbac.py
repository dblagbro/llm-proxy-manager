"""Role-based access control — Wave 6.

The existing auth layer stores a `role` string on User and Session rows.
This module adds a registry of named permissions and a role→permission
mapping, plus helpers to enforce permission-level access instead of
string-equal role checks.

Roles (initial):
    admin    — full access
    operator — read/write providers + keys, no user management
    viewer   — read-only dashboards

Named permissions (granular; new permissions are additive-only):
    providers.read, providers.write
    keys.read, keys.write, keys.reveal
    users.read, users.write
    settings.read, settings.write
    cluster.read, cluster.write
    monitoring.read
    audit.read, audit.export
    circuit_breaker.reset

Custom roles may be loaded from a config file; the in-code defaults are
the fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ── Permission catalog ───────────────────────────────────────────────────────
# Each entry is "namespace.action". These are string literals, not enums, so
# new permissions can be added without schema changes.

PROVIDERS_READ = "providers.read"
PROVIDERS_WRITE = "providers.write"
KEYS_READ = "keys.read"
KEYS_WRITE = "keys.write"
KEYS_REVEAL = "keys.reveal"
USERS_READ = "users.read"
USERS_WRITE = "users.write"
SETTINGS_READ = "settings.read"
SETTINGS_WRITE = "settings.write"
CLUSTER_READ = "cluster.read"
CLUSTER_WRITE = "cluster.write"
MONITORING_READ = "monitoring.read"
AUDIT_READ = "audit.read"
AUDIT_EXPORT = "audit.export"
CIRCUIT_BREAKER_RESET = "circuit_breaker.reset"

ALL_PERMISSIONS = frozenset({
    PROVIDERS_READ, PROVIDERS_WRITE,
    KEYS_READ, KEYS_WRITE, KEYS_REVEAL,
    USERS_READ, USERS_WRITE,
    SETTINGS_READ, SETTINGS_WRITE,
    CLUSTER_READ, CLUSTER_WRITE,
    MONITORING_READ,
    AUDIT_READ, AUDIT_EXPORT,
    CIRCUIT_BREAKER_RESET,
})


# ── Role definitions ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Role:
    name: str
    permissions: frozenset[str]
    description: str


_ADMIN = Role(
    name="admin",
    permissions=ALL_PERMISSIONS,
    description="Full access to all resources and administrative actions.",
)

_OPERATOR = Role(
    name="operator",
    permissions=frozenset({
        PROVIDERS_READ, PROVIDERS_WRITE,
        KEYS_READ, KEYS_WRITE,  # no KEYS_REVEAL
        SETTINGS_READ,           # no SETTINGS_WRITE
        CLUSTER_READ,            # no CLUSTER_WRITE
        MONITORING_READ,
        CIRCUIT_BREAKER_RESET,
    }),
    description="Read/write providers and keys; view settings and cluster state.",
)

_VIEWER = Role(
    name="viewer",
    permissions=frozenset({
        PROVIDERS_READ,
        KEYS_READ,
        SETTINGS_READ,
        CLUSTER_READ,
        MONITORING_READ,
    }),
    description="Read-only dashboards and views.",
)

_ROLES: dict[str, Role] = {
    "admin":    _ADMIN,
    "operator": _OPERATOR,
    "viewer":   _VIEWER,
}


def get_role(name: Optional[str]) -> Optional[Role]:
    if not name:
        return None
    return _ROLES.get(name.lower())


def list_roles() -> list[Role]:
    return list(_ROLES.values())


def permissions_for(role_name: Optional[str]) -> frozenset[str]:
    r = get_role(role_name)
    return r.permissions if r else frozenset()


def has_permission(role_name: Optional[str], permission: str) -> bool:
    return permission in permissions_for(role_name)
