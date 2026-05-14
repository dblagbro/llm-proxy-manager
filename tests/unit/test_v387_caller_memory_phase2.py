"""v3.8.7 (#267) Phase 2 — caller_memory + caller_memory_marker tables
+ cluster-sync wiring.

This phase ships the data layer ONLY. No memory injection at request
time, no Redis read-through, no admin endpoints — those land in later
phases. The feature flag ``caller_memory_enabled`` defaults False so
this ship is a no-op for live traffic.

Verifies:
- Tables present + indexed correctly
- Cluster-sync includes the new payload sections
- _apply_caller_memory uses LWW by updated_at
- _apply_caller_memory_markers uses monotone-extending rules
- Settings exposed in Settings UI
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Schema ─────────────────────────────────────────────────────────


def test_caller_memory_table_exists():
    from app.models.db import CallerMemory
    cols = {c.name for c in CallerMemory.__table__.columns}
    expected = {
        "id", "api_key_id", "conversation_id", "memory_tag",
        "content", "content_format",
        "updated_at", "updated_by_node",
        "source_provider_id", "source_request_id",
        "deleted_at",
    }
    missing = expected - cols
    assert not missing, f"CallerMemory missing columns: {missing}"


def test_caller_memory_marker_table_exists():
    from app.models.db import CallerMemoryMarker
    cols = {c.name for c in CallerMemoryMarker.__table__.columns}
    expected = {
        "id", "api_key_id", "conversation_id", "memory_tag",
        "first_seen_at",
        "last_known_provider_id", "last_known_external_ref",
        "recovered_at", "deleted_at",
    }
    missing = expected - cols
    assert not missing, f"CallerMemoryMarker missing columns: {missing}"


def test_caller_memory_indexed_for_keyed_lookup():
    """Memory keyed lookup is by (api_key_id, conversation_id,
    memory_tag) — those columns must be indexed for hot-path reads."""
    from app.models.db import CallerMemory
    cols = {c.name: c for c in CallerMemory.__table__.columns}
    assert cols["api_key_id"].index is True
    assert cols["conversation_id"].index is True


# ── Settings + UI exposure ─────────────────────────────────────────


def test_memory_settings_present():
    from app.config import settings
    assert hasattr(settings, "caller_memory_enabled")
    assert hasattr(settings, "caller_memory_active_flush_enabled")
    # Default OFF — ship is data-layer only
    assert settings.caller_memory_enabled is False
    # Active flush default ON — operator can opt out per RFC decision 3
    assert settings.caller_memory_active_flush_enabled is True


def test_memory_settings_exposed_in_ui_schema():
    from app.config_runtime import SCHEMA
    keys = [k for k in SCHEMA if k.startswith("caller_memory_")]
    assert len(keys) >= 2
    groups = {SCHEMA[k].get("group") for k in keys}
    assert groups == {"Caller memory"}


# ── Cluster sync payload + apply ───────────────────────────────────


def test_cluster_manager_includes_memory_in_payload():
    src = Path("app/cluster/manager.py").read_text()
    assert "CallerMemory" in src
    assert "CallerMemoryMarker" in src
    assert '"caller_memory"' in src
    assert '"caller_memory_markers"' in src


def test_cluster_sync_apply_handlers_present():
    src = Path("app/cluster/sync.py").read_text()
    assert "_apply_caller_memory" in src
    assert "_apply_caller_memory_markers" in src
    # Wired into apply_sync()
    assert 'payload.get("caller_memory", [])' in src
    assert 'payload.get("caller_memory_markers", [])' in src


def test_caller_memory_apply_uses_lww():
    """The merge code must compare timestamps and only adopt strictly-
    newer peer values (== keeps local stable on tie, avoiding ping-pong
    on identical timestamps)."""
    src = Path("app/cluster/sync.py").read_text()
    idx = src.index("async def _apply_caller_memory(")
    body = src[idx:idx + 3500]
    # LWW comparison
    assert "peer_ts > (existing.updated_at or 0)" in body
    # Tombstone propagation
    assert 'r.get("deleted_at")' in body


def test_marker_apply_uses_min_first_seen():
    """first_seen_at is the EARLIEST occurrence; sync should keep the
    minimum so a snapshot-restore that bumps it forward doesn't lose
    the original first-seen timestamp."""
    src = Path("app/cluster/sync.py").read_text()
    idx = src.index("async def _apply_caller_memory_markers")
    body = src[idx:idx + 3000]
    assert "peer_first < existing.first_seen_at" in body


def test_marker_apply_recovered_at_monotone():
    """recovered_at is monotone (None → set, never reverts)."""
    src = Path("app/cluster/sync.py").read_text()
    idx = src.index("async def _apply_caller_memory_markers")
    body = src[idx:idx + 3000]
    assert "peer_rec and not existing.recovered_at" in body


# ── No behavior change in this phase ──────────────────────────────
# (v3.8.9: Phase 4 now ships injection middleware; the two guards
# that asserted Phase 2 was data-layer-only have been retired.)


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 8, 7)
