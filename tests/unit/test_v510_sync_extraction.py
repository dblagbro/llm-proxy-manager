"""v5.0.10 — sync.py size + extraction guards.

The api_keys + providers inline merges (~451 LOC combined) were
extracted from ``app/cluster/sync.py`` into ``sync_handlers.py``
under the existing ``_apply_*`` pattern. These tests pin the
extraction so future edits don't accidentally reinline the merge
logic and bloat sync.py back over the 800-LOC trigger.
"""
from __future__ import annotations

from pathlib import Path


def test_sync_py_under_800_loc():
    """design.md trigger threshold — sync.py was 1024 LOC before
    extraction; refactor brings it under the 800 trigger."""
    src = Path("app/cluster/sync.py").read_text().splitlines()
    assert len(src) < 800, (
        f"sync.py at {len(src)} LOC — over design.md 800-LOC trigger. "
        "Likely a new inline merge was added; extract into "
        "sync_handlers._apply_<table> instead."
    )


def test_apply_sync_delegates_api_keys_to_handler():
    """The api_keys merge must call into sync_handlers, not inline
    a ``for k_data in payload.get('api_keys', [])`` loop in sync.py."""
    src = Path("app/cluster/sync.py").read_text()
    assert "_apply_api_keys(db, payload.get(\"api_keys\", []))" in src
    # No inline iteration over api_keys remains in sync.py.
    assert "for k_data in payload.get(\"api_keys\"" not in src


def test_apply_sync_delegates_providers_to_handler():
    """The providers merge must call into sync_handlers, not inline
    a ``for p_data in payload.get('providers', [])`` loop in sync.py."""
    src = Path("app/cluster/sync.py").read_text()
    assert "_apply_providers(db, payload.get(\"providers\", []))" in src
    # No inline iteration over providers remains in sync.py.
    assert "for p_data in payload.get(\"providers\"" not in src


def test_handlers_module_exports_new_helpers():
    """Both helpers must be in sync_handlers.__all__ + module-level."""
    from app.cluster import sync_handlers
    assert hasattr(sync_handlers, "_apply_api_keys")
    assert hasattr(sync_handlers, "_apply_providers")
    assert "_apply_api_keys" in sync_handlers.__all__
    assert "_apply_providers" in sync_handlers.__all__


def test_apply_api_keys_returns_peer_costs_map():
    """The extracted helper's contract: returns the per-key total_cost_usd
    dict so apply_sync can stash it in ``_peer_key_costs[source_node]``."""
    import inspect
    from app.cluster.sync_handlers import _apply_api_keys
    sig = inspect.signature(_apply_api_keys)
    params = list(sig.parameters.keys())
    assert params == ["db", "rows"], (
        f"_apply_api_keys signature drifted: {params!r}"
    )


def test_section_commit_boundaries_preserved():
    """v5.0.5 lock-release pattern: each section commits before the
    next starts. Extraction must not remove the per-section commits."""
    src = Path("app/cluster/sync.py").read_text()
    assert "_section_commit(\"api_keys\")" in src
    assert "_section_commit(\"providers\")" in src
