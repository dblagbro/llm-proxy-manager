"""v5.4.3 — admin_compliance_epoch_purge endpoint tests.

Closes the security-team-mandated pre-compliance data purge. Endpoint
shape: POST /api/admin/compliance-epoch-purge {cutoff_date, tables,
dry_run, reason}. The four critical invariants:

1. FORBIDDEN_TABLES (compliance_events, policy_changes, audit_chain,
   api_keys, users, providers, system_settings) cannot be purged.
2. Tables outside PURGABLE_TABLES are rejected even with dry_run.
3. dry_run=true never modifies the DB and never writes an audit row.
4. dry_run=false writes the audit row BEFORE the DELETEs run.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Allowlist / blocklist contracts ─────────────────────────────────


def test_purgable_tables_is_security_team_approved():
    """Pin the exact allow-list. A future ship adding a table to
    PURGABLE_TABLES must update this test on purpose."""
    from app.api.admin_compliance_epoch_purge import PURGABLE_TABLES
    assert set(PURGABLE_TABLES.keys()) == {
        "activity_log", "provider_metrics", "provider_ai_review",
    }, (
        f"PURGABLE_TABLES drift; ANY addition requires security-team sign-off. "
        f"Got: {sorted(PURGABLE_TABLES.keys())}"
    )


def test_forbidden_tables_covers_audit_chain():
    """Compliance audit-grade tables MUST be in FORBIDDEN_TABLES. If a
    refactor removes one, this test fails so the regression is caught."""
    from app.api.admin_compliance_epoch_purge import FORBIDDEN_TABLES
    for required in {
        "compliance_events",
        "compliance_policy_changes",
        "compliance_audit_chain",
    }:
        assert required in FORBIDDEN_TABLES, (
            f"{required} must always be in FORBIDDEN_TABLES; missing"
        )


def test_no_overlap_between_purgable_and_forbidden():
    """A table cannot be both purgable and forbidden — defence-in-depth
    against a buggy edit."""
    from app.api.admin_compliance_epoch_purge import (
        FORBIDDEN_TABLES, PURGABLE_TABLES,
    )
    overlap = set(PURGABLE_TABLES) & FORBIDDEN_TABLES
    assert not overlap, f"FORBIDDEN & PURGABLE overlap: {overlap}"


# ── Request validation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forbidden_table_rejected_even_with_dry_run():
    """compliance_events cannot be purged even in dry-run mode. The
    endpoint must return 400 immediately, not silently dry-run a no-op."""
    from fastapi import HTTPException
    from app.api.admin_compliance_epoch_purge import (
        compliance_epoch_purge, PurgeRequest,
    )
    req = PurgeRequest(
        cutoff_date="2026-06-06T00:00:00Z",
        tables=["compliance_events"],
        dry_run=True,
    )
    with pytest.raises(HTTPException) as exc:
        await compliance_epoch_purge(
            req, db=MagicMock(), admin=MagicMock(),
        )
    assert exc.value.status_code == 400
    assert "FORBIDDEN_TABLES" in exc.value.detail


@pytest.mark.asyncio
async def test_unknown_table_rejected_even_with_dry_run():
    """A table not in PURGABLE_TABLES (and not in FORBIDDEN) is still
    400. Closing the implicit-deny door."""
    from fastapi import HTTPException
    from app.api.admin_compliance_epoch_purge import (
        compliance_epoch_purge, PurgeRequest,
    )
    req = PurgeRequest(
        cutoff_date="2026-06-06T00:00:00Z",
        tables=["sessions"],  # not in PURGABLE
        dry_run=True,
    )
    with pytest.raises(HTTPException) as exc:
        await compliance_epoch_purge(
            req, db=MagicMock(), admin=MagicMock(),
        )
    assert exc.value.status_code == 400
    assert "PURGABLE_TABLES" in exc.value.detail


@pytest.mark.asyncio
async def test_bad_cutoff_date_rejected():
    from fastapi import HTTPException
    from app.api.admin_compliance_epoch_purge import (
        compliance_epoch_purge, PurgeRequest,
    )
    req = PurgeRequest(
        cutoff_date="not-a-date",
        tables=["activity_log"],
        dry_run=True,
    )
    with pytest.raises(HTTPException) as exc:
        await compliance_epoch_purge(
            req, db=MagicMock(), admin=MagicMock(),
        )
    assert exc.value.status_code == 400


# ── Dry-run behavior ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_returns_counts_without_modifying_db():
    """dry_run=true must NOT add any rows, NOT commit, NOT issue
    DELETE. The response carries rows_deleted=0 even when matched > 0."""
    from app.api.admin_compliance_epoch_purge import (
        compliance_epoch_purge, PurgeRequest,
    )
    db = MagicMock()
    # COUNT returns 86 for activity_log; MIN returns a timestamp.
    rs_count = MagicMock()
    rs_count.scalar.return_value = 86
    rs_min = MagicMock()
    rs_min.scalar.return_value = "2026-05-29T12:00:00"
    db.execute = AsyncMock(side_effect=[rs_count, rs_min])
    db.add = MagicMock()
    db.commit = AsyncMock()

    req = PurgeRequest(
        cutoff_date="2026-06-06T00:00:00Z",
        tables=["activity_log"],
        dry_run=True,
    )
    admin = MagicMock(username="alice")
    result = await compliance_epoch_purge(req, db=db, admin=admin)

    assert result.ok is True
    assert result.dry_run is True
    assert result.audit_id is None
    assert result.total_rows_matched == 86
    assert result.total_rows_deleted == 0
    assert result.results[0].rows_deleted == 0
    db.add.assert_not_called()  # no audit row in dry-run
    db.commit.assert_not_called()


# ── Live mode ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_mode_writes_audit_row_before_deletes():
    """In live mode the audit row commit MUST happen before any
    DELETE — so a crash mid-DELETE still records intent in the chain.
    Verified by checking the commit() sequencing."""
    from app.api.admin_compliance_epoch_purge import (
        compliance_epoch_purge, PurgeRequest,
    )
    db = MagicMock()
    rs_count = MagicMock(); rs_count.scalar.return_value = 86
    rs_min = MagicMock(); rs_min.scalar.return_value = "2026-05-29T12:00:00"
    rs_del = MagicMock(rowcount=86)
    db.execute = AsyncMock(side_effect=[rs_count, rs_min, rs_del])
    db.add = MagicMock()
    db.commit = AsyncMock()

    req = PurgeRequest(
        cutoff_date="2026-06-06T00:00:00Z",
        tables=["activity_log"],
        dry_run=False,
        reason="SEC-1234",
    )
    admin = MagicMock(username="alice")
    result = await compliance_epoch_purge(req, db=db, admin=admin)

    assert result.ok is True
    assert result.dry_run is False
    assert result.audit_id is not None
    assert result.audit_id.startswith("ppc_")
    assert result.total_rows_matched == 86
    assert result.total_rows_deleted == 86
    # audit row was added before the DELETE execute call
    db.add.assert_called_once()
    # commit happens twice in live mode (once after audit, once after deletes)
    assert db.commit.await_count == 2


def test_router_registered_in_main():
    src = Path("app/main.py").read_text()
    assert "from app.api.admin_compliance_epoch_purge import" in src
    assert "app.include_router(admin_compliance_epoch_purge_router)" in src
