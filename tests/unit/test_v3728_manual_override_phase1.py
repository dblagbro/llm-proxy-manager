"""v3.7.28 (#252 phase 1) — manual override schema + toggle/release endpoints.

Phase 1 of the AI provider supervisor adds the operator escape hatch:
when the operator clicks Disable in the UI, the provider's
``manual_override_until`` is set; the AI supervisor (Phase 4) will
read this and skip the provider entirely. Phase 1 = schema + sync +
toggle/release endpoints. Phase 2 = UI banner + 🔒 badge. Phase 4 =
the supervisor worker itself.
"""
from __future__ import annotations

from pathlib import Path


# ── Schema + migration ─────────────────────────────────────────────


def test_provider_model_has_manual_override_columns():
    from app.models.db import Provider
    cols = {c.name for c in Provider.__table__.columns}
    for col in (
        "manual_override_until",
        "manual_override_set_by",
        "manual_override_set_at",
        "manual_override_reason",
    ):
        assert col in cols, f"missing {col} on Provider model"


def test_migration_adds_manual_override_columns():
    src = Path("app/models/database.py").read_text()
    assert "ALTER TABLE providers ADD COLUMN manual_override_until" in src
    assert "ALTER TABLE providers ADD COLUMN manual_override_set_by" in src
    assert "ALTER TABLE providers ADD COLUMN manual_override_set_at" in src
    assert "ALTER TABLE providers ADD COLUMN manual_override_reason" in src


# ── Cluster sync ───────────────────────────────────────────────────


def test_cluster_manager_includes_manual_override_in_payload():
    """The outgoing sync payload (manager.py) must include all 4 lock
    fields so peers can render the banner + badge identically."""
    src = Path("app/cluster/manager.py").read_text()
    assert "manual_override_until" in src
    assert "manual_override_set_by" in src
    assert "manual_override_set_at" in src
    assert "manual_override_reason" in src


def test_cluster_sync_apply_handles_manual_override():
    """The apply_sync path (sync.py) must update existing AND create-new
    rows with the lock fields. Both branches read from p_data."""
    src = Path("app/cluster/sync.py").read_text()
    # Update branch — uses membership-test so null overwrites work
    assert '"manual_override_until" in p_data' in src
    # Create branch — passes through to Provider kwargs
    assert "manual_override_until=_parse_iso_or_none(p_data.get" in src


def test_cluster_sync_apply_handles_codex_fields():
    """v3.7.27 (#245) gap caught in v3.7.28 ship: the codex_* fields
    were added but not wired into cluster sync. Verify they're now
    replicated."""
    src = Path("app/cluster/sync.py").read_text()
    assert "codex_usage_endpoint_url" in src
    assert "codex_session_captured_at" in src
    # Cookies must NOT be synced (auth material stays on capture node)
    assert "codex_session_cookies=" not in src


# ── Toggle + release endpoints ─────────────────────────────────────


def test_toggle_endpoint_sets_manual_override_on_disable():
    """Toggling to disabled should set manual_override_until +
    set_by + set_at."""
    src = Path("app/api/providers.py").read_text()
    idx = src.index("async def toggle_provider")
    body = src[idx:idx + 3000]
    assert "manual_override_until = INDEFINITE_LOCK" in body
    assert "manual_override_set_by" in body
    assert "manual_override_set_at" in body


def test_toggle_endpoint_clears_manual_override_on_enable():
    """Toggling to enabled should release the lock."""
    src = Path("app/api/providers.py").read_text()
    idx = src.index("async def toggle_provider")
    body = src[idx:idx + 3000]
    # The enable branch sets all 4 fields back to null
    assert "manual_override_until = None" in body
    assert "manual_override_set_by = None" in body
    assert "manual_override_set_at = None" in body
    assert "manual_override_reason = None" in body


def test_toggle_response_includes_manual_override_active():
    src = Path("app/api/providers.py").read_text()
    idx = src.index("async def toggle_provider")
    body = src[idx:idx + 3000]
    assert '"manual_override_active"' in body


def test_release_manual_overrides_endpoint_exists():
    src = Path("app/api/providers.py").read_text()
    assert '@router.post("/_release-manual-overrides")' in src
    assert "async def release_manual_overrides" in src


def test_release_endpoint_clears_all_4_fields():
    src = Path("app/api/providers.py").read_text()
    idx = src.index("async def release_manual_overrides")
    body = src[idx:idx + 1500]
    assert "manual_override_until=None" in body
    assert "manual_override_set_by=None" in body
    assert "manual_override_set_at=None" in body
    assert "manual_override_reason=None" in body


def test_release_endpoint_does_not_touch_enabled():
    """Releasing the lock does NOT re-enable the provider — that's
    intentional. The supervisor will see ``enabled=False`` +
    ``manual_override_until=NULL`` and treat the provider like any
    other disabled one (it may then choose to re-enable based on
    its verdict)."""
    src = Path("app/api/providers.py").read_text()
    idx = src.index("async def release_manual_overrides")
    body = src[idx:idx + 1500]
    # The update statement must NOT touch the enabled column
    assert ".values(" in body
    update_section = body[body.index(".values("):body.index(".values(") + 600]
    assert "enabled=" not in update_section


# ── Provider serializer ────────────────────────────────────────────


def test_provider_serializer_surfaces_manual_override_fields():
    src = Path("app/api/providers.py").read_text()
    for field in (
        '"manual_override_active"',
        '"manual_override_until"',
        '"manual_override_set_by"',
        '"manual_override_set_at"',
        '"manual_override_reason"',
    ):
        assert field in src, f"missing {field} in provider serializer"


# ── Frontend wiring ────────────────────────────────────────────────


def test_frontend_provider_type_has_manual_override_fields():
    src = Path("frontend/src/types/index.ts").read_text()
    assert "manual_override_active" in src
    assert "manual_override_until" in src
    assert "manual_override_set_by" in src
    assert "manual_override_set_at" in src
    assert "manual_override_reason" in src


def test_frontend_api_has_release_call():
    src = Path("frontend/src/api/index.ts").read_text()
    assert "releaseManualOverrides" in src
    assert "/_release-manual-overrides" in src


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 28)
