"""v5.4.0 — BUG-073: zero-row audit-chain streak warning.

When the daily ``compliance_audit_worker`` signs N consecutive
zero-row days, emit one ``audit_chain_zero_row_streak`` warning to
``activity_log``. Idempotent — re-running on the same streak doesn't
multiply the noise.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest


# ── Source-grep contracts ──────────────────────────────────────────


def test_zero_row_check_helper_exists():
    from app.monitoring.compliance_audit_worker import (
        _emit_zero_row_warning_if_threshold,
        _ZERO_ROW_WARN_THRESHOLD,
    )
    assert callable(_emit_zero_row_warning_if_threshold)
    assert _ZERO_ROW_WARN_THRESHOLD >= 3, (
        f"threshold must be >= 3 days so a long weekend doesn't trigger; "
        f"got {_ZERO_ROW_WARN_THRESHOLD}"
    )


def test_sweep_calls_zero_row_check():
    """The worker's _run_one_sweep MUST call the zero-row check
    helper. Otherwise the warning never fires."""
    src = Path("app/monitoring/compliance_audit_worker.py").read_text()
    assert "await _emit_zero_row_warning_if_threshold(db, prior_day)" in src
    # And it must be inside a try/except so a failure in the warning
    # path doesn't block the next sweep
    sweep_idx = src.find("async def _run_one_sweep")
    end_idx = src.find("\nasync def ", sweep_idx + 10)
    sweep_body = src[sweep_idx:end_idx]
    assert "_emit_zero_row_warning_if_threshold" in sweep_body
    assert "except Exception" in sweep_body
    assert "zero_row_check_failed" in sweep_body


def test_audit_worker_has_heartbeat_wiring():
    """v5.4.0 also wires compliance_audit_worker to WorkerHeartbeat."""
    src = Path("app/monitoring/compliance_audit_worker.py").read_text()
    assert "from app.monitoring.worker_heartbeat import" in src
    assert "WorkerHeartbeat(name=\"compliance_audit\")" in src


# ── Behavior ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_row_check_does_not_fire_below_threshold(tmp_path, monkeypatch):
    """With fewer than _ZERO_ROW_WARN_THRESHOLD chain rows, the helper
    must NOT emit anything (a fresh deploy must not warn after 1 day)."""
    from unittest.mock import AsyncMock, MagicMock
    from app.monitoring.compliance_audit_worker import _emit_zero_row_warning_if_threshold

    db = MagicMock()
    db.execute = AsyncMock()
    # 0 rows returned
    rs = MagicMock()
    rs.scalars.return_value.all.return_value = []
    db.execute.return_value = rs

    await _emit_zero_row_warning_if_threshold(db, datetime.utcnow().date())
    # No add/commit means no warning emitted
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_zero_row_check_does_not_fire_when_any_day_has_events(monkeypatch):
    """If any of the last N days had events, no warning. The streak
    must be unbroken."""
    from unittest.mock import AsyncMock, MagicMock
    from app.monitoring.compliance_audit_worker import _emit_zero_row_warning_if_threshold

    db = MagicMock()
    db.execute = AsyncMock()
    rows = [
        MagicMock(row_count=0, day="2026-06-12"),
        MagicMock(row_count=5, day="2026-06-11"),   # broke the streak
        MagicMock(row_count=0, day="2026-06-10"),
    ]
    rs = MagicMock()
    rs.scalars.return_value.all.return_value = rows
    db.execute.return_value = rs

    await _emit_zero_row_warning_if_threshold(db, datetime.utcnow().date())
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_zero_row_check_fires_on_unbroken_streak(monkeypatch):
    """N consecutive zero-row days WITH no existing warning row in the
    last 24h → exactly one ActivityLog row added."""
    from unittest.mock import AsyncMock, MagicMock
    from app.monitoring.compliance_audit_worker import _emit_zero_row_warning_if_threshold

    db = MagicMock()
    rows = [
        MagicMock(row_count=0, day="2026-06-12"),
        MagicMock(row_count=0, day="2026-06-11"),
        MagicMock(row_count=0, day="2026-06-10"),
    ]
    rs_chain = MagicMock()
    rs_chain.scalars.return_value.all.return_value = rows

    # Second execute() call is the duplicate-check; return None
    rs_dup = MagicMock()
    rs_dup.scalar_one_or_none.return_value = None

    db.execute = AsyncMock(side_effect=[rs_chain, rs_dup])
    db.commit = AsyncMock()

    await _emit_zero_row_warning_if_threshold(db, datetime.utcnow().date())
    db.add.assert_called_once()
    # The added object should be an ActivityLog
    args, _ = db.add.call_args
    added = args[0]
    assert added.event_type == "audit_chain_zero_row_streak"
    assert added.severity == "warning"
    assert "streak_start=2026-06-10" in added.message
    assert "streak_end=2026-06-12" in added.message


@pytest.mark.asyncio
async def test_zero_row_check_is_idempotent_within_24h(monkeypatch):
    """If a warning for the same streak was already emitted in the
    last 24h, the helper must NOT emit a duplicate."""
    from unittest.mock import AsyncMock, MagicMock
    from app.monitoring.compliance_audit_worker import _emit_zero_row_warning_if_threshold

    db = MagicMock()
    rows = [
        MagicMock(row_count=0, day="2026-06-12"),
        MagicMock(row_count=0, day="2026-06-11"),
        MagicMock(row_count=0, day="2026-06-10"),
    ]
    rs_chain = MagicMock()
    rs_chain.scalars.return_value.all.return_value = rows

    # Duplicate-check returns an existing row → suppress
    rs_dup = MagicMock()
    rs_dup.scalar_one_or_none.return_value = MagicMock()  # an existing row

    db.execute = AsyncMock(side_effect=[rs_chain, rs_dup])

    await _emit_zero_row_warning_if_threshold(db, datetime.utcnow().date())
    db.add.assert_not_called()
