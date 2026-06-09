"""v5.3.3 — direct coverage of the BoolSystemSetting factory.

The two shim modules (logging_controls, llm_emergency_stop) already
have integration-level tests pinning their behavior; this file targets
the factory itself so unit-level regressions are obvious in CI.

Pin checks:
- factory module exists with the expected public surface
- TTL cache behaves (cold miss, warm hit, eager invalidate)
- get_session_less() opens its own session
- set() writes the audit row, returns the contract dict, and
  invalidates the cache
- fail-OPEN returns the configured default on DB error
- on/off labels surface in the audit reason text
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)


async def _fresh_db():
    from app.models.db import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _make_setting(*, key="compliance.test_flag", default=False):
    from app.monitoring._bool_system_setting import BoolSystemSetting
    return BoolSystemSetting(
        setting_key=key,
        default=default,
        on_label="ENGAGED",
        off_label="DISENGAGED",
        audit_subject="test_flag",
        log_prefix="test_flag.toggled",
        ttl_sec=30.0,
    )


# ── Module-level pins ───────────────────────────────────────────────


def test_factory_module_exists():
    from app.monitoring._bool_system_setting import BoolSystemSetting
    for attr in ("get", "get_session_less", "set", "invalidate_cache"):
        assert hasattr(BoolSystemSetting, attr), f"missing {attr}"


def test_shims_both_use_the_factory():
    """Both bool toggles should now be thin shims over the factory,
    not duplicate TTL-cache + audit machinery."""
    lc = Path("app/monitoring/logging_controls.py").read_text()
    es = Path("app/monitoring/llm_emergency_stop.py").read_text()
    for src, label in ((lc, "logging_controls"), (es, "llm_emergency_stop")):
        assert "BoolSystemSetting(" in src, f"{label} doesn't construct the factory"
        assert "_setting.get" in src or "_setting.set" in src, \
            f"{label} doesn't route through the factory"


# ── Behavioral ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_default_when_row_absent():
    s = _make_setting(default=True)
    Session = await _fresh_db()
    async with Session() as db:
        assert await s.get(db) is True
    s2 = _make_setting(default=False)
    async with Session() as db:
        assert await s2.get(db) is False


@pytest.mark.asyncio
async def test_set_then_get_roundtrip():
    s = _make_setting()
    Session = await _fresh_db()
    async with Session() as db:
        result = await s.set(db, enabled=True, actor="qa", reason="t")
        assert result["ok"] is True
        assert result["enabled"] is True
        assert result["prior_state"] is False
        assert result["noop"] is False
        assert "audit_id" in result and result["audit_id"]
        # Read-back via get() reflects the new value
        assert await s.get(db) is True


@pytest.mark.asyncio
async def test_noop_flip_still_audits():
    """Re-engaging an already-engaged flag MUST still write the audit
    row + mark the result as noop. Preserves the pre-refactor contract."""
    from app.models.db import CompliancePolicyChange
    from sqlalchemy import select, func

    s = _make_setting()
    Session = await _fresh_db()
    async with Session() as db:
        await s.set(db, enabled=True, actor="qa")
        result = await s.set(db, enabled=True, actor="qa")
        assert result["noop"] is True
        n = (await db.execute(
            select(func.count()).select_from(CompliancePolicyChange)
        )).scalar_one()
        assert n == 2


@pytest.mark.asyncio
async def test_audit_row_carries_on_off_labels():
    """The labels passed to the factory must surface in the audit
    summary so an operator scanning policy_change.reason can see at a
    glance which toggle fired and which direction."""
    from app.models.db import CompliancePolicyChange
    from sqlalchemy import select

    s = _make_setting()  # ENGAGED/DISENGAGED in the helper
    Session = await _fresh_db()
    async with Session() as db:
        await s.set(db, enabled=True, actor="alice", reason="incident-x")
        row = (await db.execute(select(CompliancePolicyChange))).scalar_one()
        assert "ENGAGED" in row.reason
        assert "alice" in row.reason
        assert "incident-x" in row.reason
        assert row.scope == "system"


@pytest.mark.asyncio
async def test_cache_invalidates_on_set():
    """Eager cache invalidate so the local node honors the flip on the
    very next get() — matches the pre-refactor invariant. No 30s
    cache lag on the node that just received the toggle."""
    s = _make_setting()
    Session = await _fresh_db()
    async with Session() as db:
        # Prime the cache with False
        assert await s.get(db) is False
        # Flip + read again, expect True immediately
        await s.set(db, enabled=True, actor="qa")
        assert await s.get(db) is True


@pytest.mark.asyncio
async def test_invalidate_cache_picks_up_external_writes():
    """Simulates cluster-sync apply: a peer's value lands directly in
    system_settings. Without invalidate, the local cache hides it; with
    invalidate, the new value surfaces immediately."""
    from app.models.db import SystemSetting

    s = _make_setting()
    Session = await _fresh_db()
    async with Session() as db:
        assert await s.get(db) is False  # prime False
        db.add(SystemSetting(
            key=s.setting_key, value="true",
            value_type="bool", updated_at=0.0,
        ))
        await db.commit()
        # Cached False is still served
        assert await s.get(db) is False
        s.invalidate_cache()
        assert await s.get(db) is True


@pytest.mark.asyncio
async def test_get_session_less_opens_own_session():
    """The session-less variant for background callers
    (acompletion_with_retry path) must work without a db arg."""
    import app.models.database as _dbmod
    s = _make_setting(default=False)
    Session = await _fresh_db()
    async with Session() as db:
        await s.set(db, enabled=True, actor="qa")
    s.invalidate_cache()  # force a DB read
    _saved = _dbmod.AsyncSessionLocal
    _dbmod.AsyncSessionLocal = Session
    try:
        assert await s.get_session_less() is True
    finally:
        _dbmod.AsyncSessionLocal = _saved


@pytest.mark.asyncio
async def test_fail_open_returns_default_on_db_error():
    """When the DB raises, the configured default wins. Both production
    shims rely on this (logging_controls fails OPEN to keep observability
    visible; llm_emergency_stop fails OPEN to never halt traffic on a
    transient DB issue)."""
    from unittest.mock import patch, AsyncMock

    s_true = _make_setting(default=True)
    s_false = _make_setting(default=False, key="compliance.other")
    Session = await _fresh_db()
    async with Session() as db:
        with patch.object(s_true, "_read_from_db", new=AsyncMock(side_effect=RuntimeError("boom"))):
            assert await s_true.get(db) is True
        with patch.object(s_false, "_read_from_db", new=AsyncMock(side_effect=RuntimeError("boom"))):
            assert await s_false.get(db) is False
