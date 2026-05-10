"""v3.7.15 — cluster sync for the three v3.7.x tables (BUG-016)
and cross-node IP-block cache invalidation (BUG-018)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── BUG-016: payload contains the three new sections ─────────────


def test_sync_payload_includes_blocked_ips_section():
    """_build_sync_payload must emit a `blocked_ips` key."""
    from pathlib import Path
    src = Path("app/cluster/manager.py").read_text()
    assert '"blocked_ips":' in src
    assert "BlockedIp" in src


def test_sync_payload_includes_api_key_ai_reviews_section():
    from pathlib import Path
    src = Path("app/cluster/manager.py").read_text()
    assert '"api_key_ai_reviews":' in src
    assert "ApiKeyAiReview" in src


def test_sync_payload_includes_external_usage_snapshots_section():
    from pathlib import Path
    src = Path("app/cluster/manager.py").read_text()
    assert '"external_usage_snapshots":' in src
    assert "ExternalUsageSnapshot" in src


def test_sync_payload_blocked_ips_includes_tombstone_field():
    """Without `deleted_at` in the payload, peers can't learn about deletions."""
    from pathlib import Path
    src = Path("app/cluster/manager.py").read_text()
    # Find the blocked_ips_payload list comprehension and confirm deleted_at
    # is one of the emitted fields.
    idx = src.index("blocked_ips_payload =")
    # Range covers the list comprehension
    snippet = src[idx:idx + 700]
    assert "b.deleted_at" in snippet, "blocked_ips payload missing deleted_at field"


# ── BUG-016: apply_sync calls the new handlers ─────────────────────


def test_apply_sync_invokes_blocked_ips_handler():
    from pathlib import Path
    src = Path("app/cluster/sync.py").read_text()
    assert "_apply_blocked_ips" in src
    assert "_apply_ai_reviews" in src
    assert "_apply_external_usage_snapshots" in src


def test_apply_sync_handlers_defined():
    """The three new merge helpers must exist."""
    from app.cluster import sync
    assert hasattr(sync, "_apply_blocked_ips")
    assert hasattr(sync, "_apply_ai_reviews")
    assert hasattr(sync, "_apply_external_usage_snapshots")


# ── BUG-018: cache invalidation on receiving sync ──────────────────


def test_apply_sync_invalidates_ip_block_cache_when_changed():
    """If _apply_blocked_ips returns True, apply_sync must call
    _clear_cache_for_tests so peer nodes see the new block list
    on the next request (not 30s later)."""
    from pathlib import Path
    src = Path("app/cluster/sync.py").read_text()
    # The invalidation call must be reachable from apply_sync after
    # blocked_ips_changed is True
    assert "_clear_cache_for_tests" in src
    assert "blocked_ips_changed" in src


def test_blocked_ips_handler_returns_bool_change_signal():
    """_apply_blocked_ips must return a bool — True iff any row was
    inserted, updated, or tombstoned (so caller can invalidate cache)."""
    from pathlib import Path
    src = Path("app/cluster/sync.py").read_text()
    # Function signature returns bool
    assert "async def _apply_blocked_ips(db: AsyncSession, rows: list[dict]) -> bool:" in src
    assert "changed = False" in src
    assert "return changed" in src


# ── Soft-delete tombstone model + migration ────────────────────────


def test_blocked_ip_has_deleted_at_column():
    from app.models.db import BlockedIp
    cols = {c.name for c in BlockedIp.__table__.columns}
    assert "deleted_at" in cols, "BlockedIp missing deleted_at — tombstones won't sync"


def test_blocked_ips_migration_present():
    """Idempotent ALTER TABLE for the new deleted_at column."""
    from pathlib import Path
    src = Path("app/models/database.py").read_text()
    assert "ALTER TABLE blocked_ips ADD COLUMN deleted_at" in src


# ── Admin DELETE is soft-delete ────────────────────────────────────


def test_admin_delete_is_soft_delete():
    """The DELETE endpoint must set deleted_at, not hard-delete the row,
    so the tombstone propagates through cluster sync."""
    from pathlib import Path
    src = Path("app/api/blocked_ips.py").read_text()
    # Find the delete endpoint
    idx = src.index("async def remove_blocked_ip")
    endpoint_body = src[idx:idx + 1200]
    assert "existing.deleted_at" in endpoint_body
    # Must NOT execute a SQL DELETE (hard delete)
    assert "delete(BlockedIp).where" not in endpoint_body or \
           endpoint_body.index("existing.deleted_at") < endpoint_body.index("delete(BlockedIp)")


def test_admin_list_filters_tombstoned():
    """GET endpoint must hide soft-deleted rows."""
    from pathlib import Path
    src = Path("app/api/blocked_ips.py").read_text()
    idx = src.index("async def list_blocked_ips")
    endpoint_body = src[idx:idx + 700]
    assert "deleted_at.is_(None)" in endpoint_body


def test_middleware_loader_filters_tombstoned():
    """_load_blocked_set must filter deleted_at IS NULL or peers'
    tombstones would keep blocking traffic after sync."""
    from pathlib import Path
    src = Path("app/middleware/ip_block.py").read_text()
    assert "deleted_at.is_(None)" in src


# ── Tombstone propagation logic ────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_blocked_ips_inserts_new_row():
    from app.cluster.sync import _apply_blocked_ips
    db = MagicMock()
    db.execute = AsyncMock()
    rs = MagicMock()
    rs.scalar_one_or_none = MagicMock(return_value=None)
    db.execute.return_value = rs
    db.add = MagicMock()
    rows = [{"ip": "1.2.3.4", "reason": "abuse",
             "added_at": "2026-05-10T10:00:00+00:00",
             "added_by": "admin", "deleted_at": None}]
    changed = await _apply_blocked_ips(db, rows)
    assert changed is True
    db.add.assert_called_once()


@pytest.mark.asyncio
async def test_apply_blocked_ips_tombstone_propagation():
    """Peer says deleted; local has a live row → adopt the tombstone."""
    from app.cluster.sync import _apply_blocked_ips
    from app.models.db import BlockedIp
    existing = BlockedIp(
        ip="1.2.3.4", reason="local", added_by="admin",
        added_at=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        deleted_at=None,
    )
    db = MagicMock()
    db.execute = AsyncMock()
    rs = MagicMock()
    rs.scalar_one_or_none = MagicMock(return_value=existing)
    db.execute.return_value = rs
    rows = [{"ip": "1.2.3.4", "reason": "local", "added_by": "admin",
             "added_at": "2026-05-10T10:00:00+00:00",
             "deleted_at": "2026-05-10T12:00:00+00:00"}]
    changed = await _apply_blocked_ips(db, rows)
    assert changed is True
    assert existing.deleted_at is not None


@pytest.mark.asyncio
async def test_apply_blocked_ips_skips_stale_peer_row():
    """Peer row's added_at is older than local — no change."""
    from app.cluster.sync import _apply_blocked_ips
    from app.models.db import BlockedIp
    existing = BlockedIp(
        ip="1.2.3.4", reason="local", added_by="admin",
        added_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        deleted_at=None,
    )
    db = MagicMock()
    db.execute = AsyncMock()
    rs = MagicMock()
    rs.scalar_one_or_none = MagicMock(return_value=existing)
    db.execute.return_value = rs
    rows = [{"ip": "1.2.3.4", "reason": "old", "added_by": "olduser",
             "added_at": "2026-05-10T10:00:00+00:00",  # earlier
             "deleted_at": None}]
    changed = await _apply_blocked_ips(db, rows)
    assert changed is False
    # local row preserved
    assert existing.reason == "local"
    assert existing.added_by == "admin"


@pytest.mark.asyncio
async def test_apply_blocked_ips_rearms_after_tombstone():
    """Peer's added_at is newer + no tombstone → clear local tombstone."""
    from app.cluster.sync import _apply_blocked_ips
    from app.models.db import BlockedIp
    existing = BlockedIp(
        ip="1.2.3.4", reason="old", added_by="admin",
        added_at=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        deleted_at=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
    )
    db = MagicMock()
    db.execute = AsyncMock()
    rs = MagicMock()
    rs.scalar_one_or_none = MagicMock(return_value=existing)
    db.execute.return_value = rs
    rows = [{"ip": "1.2.3.4", "reason": "new", "added_by": "admin2",
             "added_at": "2026-05-10T12:00:00+00:00",
             "deleted_at": None}]
    changed = await _apply_blocked_ips(db, rows)
    assert changed is True
    assert existing.deleted_at is None
    assert existing.reason == "new"


# ── Add re-arm path through admin POST ─────────────────────────────


def test_admin_post_rearms_tombstoned_row():
    """POST on a tombstoned IP must clear deleted_at + bump added_at."""
    from pathlib import Path
    src = Path("app/api/blocked_ips.py").read_text()
    idx = src.index("async def add_blocked_ip")
    endpoint_body = src[idx:idx + 2000]
    assert "existing.deleted_at = None" in endpoint_body
    assert "existing.added_at = " in endpoint_body


def test_version_bumped_to_3_7_15():
    from app.__version__ import __version__
    assert __version__ == "3.7.15"
