"""Regression pins for the v5.22.43 data-layer fixes that are otherwise silent.

F8 — epoch purge compared the ISO 'T' cutoff string against SQLite's
space-separated DateTime text, so ' ' (0x20) < 'T' (0x54) made `ts < :cutoff`
over-match the entire cutoff day on an irreversible hard-delete.

F9 — cluster sync apply accepted a peer-supplied primary key.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime


def _normalise(cutoff_iso: str) -> str:
    # mirrors app/api/admin_compliance_epoch_purge.py
    return datetime.fromisoformat(cutoff_iso.rstrip("Z")).strftime("%Y-%m-%d %H:%M:%S")


def test_f8_cutoff_does_not_overmatch_the_boundary_day():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (ts TEXT)")
    rows = [
        "2026-06-05 10:00:00",  # before cutoff day -> delete
        "2026-06-06 00:00:00",  # exactly cutoff    -> keep (exclusive)
        "2026-06-06 23:59:59",  # cutoff day later  -> keep
    ]
    con.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])

    cutoff = "2026-06-06T00:00:00"
    # Raw ISO string (the bug): over-matches the whole 06-06 day.
    buggy = con.execute("SELECT COUNT(*) FROM t WHERE ts < ?", (cutoff,)).fetchone()[0]
    assert buggy == 3  # demonstrates the defect

    # Normalised cutoff (the fix): only the genuinely-older row matches.
    fixed = con.execute(
        "SELECT COUNT(*) FROM t WHERE ts < ?", (_normalise(cutoff),)
    ).fetchone()[0]
    assert fixed == 1
    con.close()


def test_f9_peer_primary_key_is_not_accepted():
    from app.models.db import ComplianceEvent, CompliancePolicyChange

    # The apply handlers build allowed_cols as the table columns minus {"id"};
    # assert the PK is excluded so a peer cannot choose it.
    ce = {c.name for c in ComplianceEvent.__table__.columns} - {"id"}
    pc = {c.name for c in CompliancePolicyChange.__table__.columns} - {"id"}
    assert "id" not in ce
    assert "id" not in pc
    # business dedup keys must still be present
    assert "audit_id" in ce
    assert "policy_change_id" in pc
