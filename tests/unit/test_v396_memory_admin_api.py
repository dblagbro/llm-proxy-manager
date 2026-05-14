"""v3.9.6 (#267) Phase 9 — admin API for caller_memory + markers.

Source-level guards (route registration + handler shape) — no live
endpoint testing because admin endpoints require an authenticated
admin session and the test seed there is non-trivial. The integration
test suite covers the auth-wrapped path.
"""
from __future__ import annotations

from pathlib import Path


# ── Module + router ────────────────────────────────────────────────


def test_module_imports_cleanly():
    import importlib
    mod = importlib.import_module("app.api.memory_admin")
    assert hasattr(mod, "router")


def test_router_registered_in_main():
    src = Path("app/main.py").read_text()
    assert "from app.api.memory_admin import router as memory_admin_router" in src
    assert "app.include_router(memory_admin_router)" in src


def test_router_prefix_is_api_memory():
    from app.api.memory_admin import router
    assert router.prefix == "/api/memory"


# ── Endpoints ──────────────────────────────────────────────────────


def test_all_endpoints_registered():
    from app.api.memory_admin import router
    paths = {(r.path, tuple(sorted(r.methods))) for r in router.routes}

    expected = {
        ("/api/memory/keys/{api_key_id}", ("GET",)),
        ("/api/memory/keys/{api_key_id}/{memory_tag}", ("PUT",)),
        ("/api/memory/keys/{api_key_id}/{memory_tag}", ("DELETE",)),
        ("/api/memory/markers/{api_key_id}", ("GET",)),
        ("/api/memory/markers/{marker_id}/clear-recovered", ("POST",)),
        ("/api/memory/recover/{api_key_id}/{conversation_id}/{memory_tag}",
         ("POST",)),
    }
    for exp in expected:
        assert exp in paths, f"missing route {exp}"


def test_all_endpoints_require_admin():
    """Every route uses Depends(require_admin)."""
    src = Path("app/api/memory_admin.py").read_text()
    # Count the number of route decorators
    decorators = src.count("@router.")
    # Count the number of require_admin dependencies
    admin_deps = src.count("Depends(require_admin)")
    assert decorators >= 6
    assert admin_deps == decorators, (
        f"some routes missing require_admin: {decorators} routes, "
        f"{admin_deps} admin deps"
    )


# ── Provider edit form picks up memory_disabled ─────────────────────


def test_provider_create_schema_includes_memory_disabled():
    from app.api.providers import ProviderCreate
    fields = ProviderCreate.model_fields
    assert "memory_disabled" in fields


def test_provider_serialize_emits_memory_disabled():
    src = Path("app/api/providers.py").read_text()
    assert '"memory_disabled":' in src


# ── Wire to underlying store ───────────────────────────────────────


def test_upsert_uses_store_put():
    src = Path("app/api/memory_admin.py").read_text()
    assert "from app.memory.store import put" in src
    assert "await put(" in src


def test_delete_uses_store_delete():
    src = Path("app/api/memory_admin.py").read_text()
    assert "from app.memory.store import delete as store_delete" in src
    assert "await store_delete(" in src


def test_trigger_recovery_uses_phase_7_dispatcher():
    src = Path("app/api/memory_admin.py").read_text()
    assert "from app.memory.recover import maybe_recover_memory" in src
    assert "await maybe_recover_memory(" in src


def test_clear_recovered_sets_recovered_at_to_none():
    """Phase 7 retry semantics — recovered_at=None means try again next request."""
    src = Path("app/api/memory_admin.py").read_text()
    idx = src.index("async def clear_marker_recovered_at")
    body = src[idx:idx + 1500]
    assert "row.recovered_at = None" in body
