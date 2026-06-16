"""v5.7.11 — operator-controllable suppression of the audit-chain
zero-row-streak warning on instances without enforcement-eligible
traffic."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_zero_row_warning_suppressed_when_setting_false():
    """When system_settings has compliance_audit.zero_row_warning_enabled
    = false, the worker MUST short-circuit before reading the chain
    rows. Used on instances where the canary moved away and the zero
    row count is operationally expected."""
    from app.monitoring.compliance_audit_worker import _emit_zero_row_warning_if_threshold

    # First execute returns the setting row with value=false
    setting_row = MagicMock()
    setting_row.value = "false"
    setting_rs = MagicMock()
    setting_rs.scalar_one_or_none.return_value = setting_row

    db = MagicMock()
    db.execute = AsyncMock(return_value=setting_rs)
    db.add = MagicMock()
    db.commit = AsyncMock()

    await _emit_zero_row_warning_if_threshold(db, "2026-06-15")

    # Only the setting probe should run; chain query must NOT
    assert db.execute.await_count == 1, (
        "When suppression is active, the worker must not query "
        "compliance_audit_chain at all."
    )
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_zero_row_warning_fires_when_setting_absent():
    """Default behavior unchanged: setting absent → warning still
    fires per the v5.4.1 design."""
    from app.monitoring.compliance_audit_worker import _emit_zero_row_warning_if_threshold

    # 1st execute: setting probe returns None (absent)
    setting_rs = MagicMock(); setting_rs.scalar_one_or_none.return_value = None
    # 2nd execute: chain rows — 3 zero-row days
    chain_row = MagicMock(); chain_row.row_count = 0; chain_row.day = "2026-06-15"
    chain_row2 = MagicMock(); chain_row2.row_count = 0; chain_row2.day = "2026-06-14"
    chain_row3 = MagicMock(); chain_row3.row_count = 0; chain_row3.day = "2026-06-13"
    chain_rs = MagicMock()
    chain_rs.scalars.return_value.all.return_value = [chain_row, chain_row2, chain_row3]
    # 3rd execute: dedup probe → no existing warning
    dedup_rs = MagicMock(); dedup_rs.scalar_one_or_none.return_value = None

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[setting_rs, chain_rs, dedup_rs])
    db.add = MagicMock()
    db.commit = AsyncMock()

    await _emit_zero_row_warning_if_threshold(db, "2026-06-15")
    db.add.assert_called_once()  # the warning row was written


@pytest.mark.asyncio
async def test_zero_row_warning_fires_when_setting_explicitly_true():
    """Setting present but value=true → behave like default (warn).
    Catches a misconfiguration where someone sets the key without
    understanding the off semantics."""
    from app.monitoring.compliance_audit_worker import _emit_zero_row_warning_if_threshold

    setting_row = MagicMock(); setting_row.value = "true"
    setting_rs = MagicMock(); setting_rs.scalar_one_or_none.return_value = setting_row
    chain_row = MagicMock(); chain_row.row_count = 0; chain_row.day = "2026-06-15"
    chain_rs = MagicMock()
    chain_rs.scalars.return_value.all.return_value = [chain_row, chain_row, chain_row]
    dedup_rs = MagicMock(); dedup_rs.scalar_one_or_none.return_value = None

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[setting_rs, chain_rs, dedup_rs])
    db.add = MagicMock()
    db.commit = AsyncMock()

    await _emit_zero_row_warning_if_threshold(db, "2026-06-15")
    db.add.assert_called_once()


def test_zero_row_warning_module_documents_opt_out_key():
    """Pin the contract: the setting key name lives in the source so
    operators / runbooks can reference it."""
    from pathlib import Path
    src = Path("app/monitoring/compliance_audit_worker.py").read_text()
    assert "compliance_audit.zero_row_warning_enabled" in src
