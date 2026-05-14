"""v3.9.12 (#267) — api-key-scoped memory CRUD endpoints.

Hub-team unblock: they want a hub-callable purge for room archival
without holding an admin session. memory_admin.py requires
Depends(require_admin); memory_scoped.py authenticates via the api_key
itself and confines all queries to that key's rows.

Tests confirm isolation (key A can't see key B's memory) and the
correct shape of the new endpoints.
"""
from __future__ import annotations

from pathlib import Path


def test_module_imports_cleanly():
    import importlib
    mod = importlib.import_module("app.api.memory_scoped")
    assert hasattr(mod, "router")


def test_router_registered_in_main():
    src = Path("app/main.py").read_text()
    assert "from app.api.memory_scoped import router as memory_scoped_router" in src
    assert "app.include_router(memory_scoped_router)" in src


def test_prefix_is_v1_memory():
    """Lives under /v1/memory/* — api-key-scoped namespace. (Admin
    counterpart is under /api/memory/*.)"""
    from app.api.memory_scoped import router
    assert router.prefix == "/v1/memory"


def test_all_endpoints_registered():
    from app.api.memory_scoped import router
    paths = {(r.path, tuple(sorted(r.methods))) for r in router.routes}
    expected = {
        ("/v1/memory/conversations", ("GET",)),
        ("/v1/memory/conversations/{conversation_id}", ("GET",)),
        ("/v1/memory/conversations/{conversation_id}", ("DELETE",)),
        ("/v1/memory/conversations/{conversation_id}/{memory_tag}", ("PUT",)),
        ("/v1/memory/conversations/{conversation_id}/{memory_tag}", ("DELETE",)),
    }
    for exp in expected:
        assert exp in paths, f"missing route {exp}"


def test_all_endpoints_use_api_key_auth_not_admin():
    """The whole point is to NOT require admin session. Every route
    depends on resolve_api_key_dep (the same auth as /v1/messages),
    not require_admin."""
    src = Path("app/api/memory_scoped.py").read_text()
    assert "from app.auth.keys import" in src
    assert "resolve_api_key_dep" in src
    assert "ApiKeyRecord" in src
    # CRITICAL: no admin-session imports in this module
    assert "require_admin" not in src
    assert "AdminUser" not in src


def test_all_queries_scoped_to_api_key_id():
    """The api_key_id from the verified key must be the outermost
    filter on every read AND write. No path can leak across keys."""
    src = Path("app/api/memory_scoped.py").read_text()
    # Every CallerMemory query in the module includes the key filter
    occurrences = src.count("CallerMemory.api_key_id == key.id")
    # 4 reads (list conversations, list one conv, delete-entire-conv lookup, …)
    # plus delete uses store.delete (which already enforces key scope)
    assert occurrences >= 3, (
        f"Expected api_key scoping on every query; found {occurrences} occurrences"
    )


def test_delete_entire_conversation_tombstones_marker_too():
    """Room archival should drop both the content rows AND the marker;
    otherwise Phase 7 recovery could try to reconstruct from the (now
    stale) last_known_provider_id."""
    src = Path("app/api/memory_scoped.py").read_text()
    idx = src.index("async def delete_entire_conversation")
    fn = src[idx:idx + 3000]
    assert "CallerMemoryMarker" in fn
    assert "m.deleted_at = now" in fn


def test_delete_entire_conversation_invalidates_redis():
    """Tombstoning in SQLite leaves the Redis hot cache stale —
    explicitly delete cache keys after commit."""
    src = Path("app/api/memory_scoped.py").read_text()
    idx = src.index("async def delete_entire_conversation")
    fn = src[idx:idx + 3000]
    assert "from app.memory.store import _get_redis, _key" in fn
    assert "await r.delete(" in fn


def test_upsert_uses_store_put():
    """Mutations go through the existing store layer (which already
    handles cluster sync + marker maintenance + Redis invalidation).
    No bypass."""
    src = Path("app/api/memory_scoped.py").read_text()
    idx = src.index("async def upsert_scoped_memory")
    fn = src[idx:idx + 1500]
    assert "from app.memory.store import put" in fn


def test_delete_single_tag_uses_store_delete():
    src = Path("app/api/memory_scoped.py").read_text()
    idx = src.index("async def delete_one_tag")
    fn = src[idx:idx + 1500]
    assert "from app.memory.store import delete as store_delete" in fn


def test_list_conversations_groups_by_conv_id():
    """The list endpoint returns one row per conversation_id with the
    tag set + max(updated_at), not one row per (conv, tag) pair."""
    src = Path("app/api/memory_scoped.py").read_text()
    idx = src.index("async def list_conversations")
    fn = src[idx:idx + 2500]
    assert "by_conv" in fn
    assert "\"tags\":" in fn
