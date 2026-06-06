"""v5.1.2 / Batch C3 — retention editable in WebUI.

Operator can override the env defaults via system_settings; prune
sweep refreshes the cache before each pass so an edit takes effect
within one sweep cycle (default ~24h).
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


# ── Source pins ─────────────────────────────────────────────────────


def test_retention_settings_module_exists():
    from app.monitoring import retention_settings as rs
    for fn in (rs.info_days, rs.warning_days, rs.error_days,
               rs.get_retention_days, rs.set_retention,
               rs.refresh_from_db, rs.current_state):
        assert callable(fn)
    for k in (rs.KEY_INFO, rs.KEY_WARNING, rs.KEY_ERROR):
        assert k.startswith("compliance.activity_log_")


def test_prune_helpers_read_from_retention_settings():
    src = Path("app/monitoring/prune.py").read_text()
    assert "from app.monitoring.retention_settings import info_days" in src
    assert "from app.monitoring.retention_settings import warning_days" in src
    assert "from app.monitoring.retention_settings import error_days" in src


def test_sweep_refreshes_cache_before_reading():
    """_sweep_once must call refresh_from_db before reading the
    retention values; otherwise a UI edit doesn't land until the
    soft TTL elapses (60s — fine in steady state but observably
    inconsistent right after a flip)."""
    src = Path("app/monitoring/prune.py").read_text()
    sweep_idx = src.find("async def _sweep_once")
    assert sweep_idx != -1
    body = src[sweep_idx:sweep_idx + 1500]
    refresh_idx = body.find("refresh_from_db")
    keep_idx = body.find("_retention_days()")
    assert refresh_idx != -1 and keep_idx != -1
    assert refresh_idx < keep_idx, (
        "refresh_from_db must be called BEFORE _retention_days() "
        "reads — otherwise the sweep uses stale values."
    )


def test_admin_endpoints_registered():
    src = Path("app/api/admin_logging.py").read_text()
    assert '@router.get("/retention")' in src
    assert '@router.post("/retention")' in src


def test_set_retention_writes_audit_row():
    src = Path("app/monitoring/retention_settings.py").read_text()
    assert "CompliancePolicyChange" in src
    assert 'scope="system"' in src


# ── Behavioral ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_retention_persists_and_reads_back():
    from app.monitoring import retention_settings as rs
    Session = await _fresh_db()
    async with Session() as db:
        await rs.refresh_from_db(db)
        # Override info to 7d (down from 30d env default)
        result = await rs.set_retention(
            db, rs.KEY_INFO, days=7, actor="qa", reason="t",
        )
        assert result["new_override"] == 7
        assert result["effective_days"] == 7
        # Re-read via the sync getter (matches what prune.py does)
        assert rs.info_days() == 7
        # Now CLEAR the override
        result2 = await rs.set_retention(
            db, rs.KEY_INFO, days=None, actor="qa", reason="undo",
        )
        assert result2["new_override"] is None
        # Effective drops back to env default
        assert rs.info_days() == rs._env_default(rs.KEY_INFO)


@pytest.mark.asyncio
async def test_set_retention_rejects_zero_or_negative():
    from app.monitoring import retention_settings as rs
    Session = await _fresh_db()
    async with Session() as db:
        with pytest.raises(ValueError):
            await rs.set_retention(db, rs.KEY_INFO, days=0, actor="qa")
        with pytest.raises(ValueError):
            await rs.set_retention(db, rs.KEY_INFO, days=-5, actor="qa")


@pytest.mark.asyncio
async def test_set_retention_rejects_unknown_key():
    from app.monitoring import retention_settings as rs
    Session = await _fresh_db()
    async with Session() as db:
        with pytest.raises(ValueError):
            await rs.set_retention(db, "bogus.key", days=30, actor="qa")


@pytest.mark.asyncio
async def test_refresh_from_db_picks_up_changes():
    """After a direct DB write (simulating cluster sync from a peer),
    refresh_from_db propagates the new value into the sync getter."""
    from app.monitoring import retention_settings as rs
    from app.models.db import SystemSetting
    Session = await _fresh_db()
    async with Session() as db:
        # Direct write — bypass set_retention so this matches the
        # cluster-sync-from-peer code path.
        db.add(SystemSetting(
            key=rs.KEY_WARNING, value="42", value_type="int",
            updated_at=0.0,
        ))
        await db.commit()
        # Stale cache still reports None — confirm getter returns env
        # default until refresh runs.
        assert rs.warning_days() == rs._env_default(rs.KEY_WARNING)
        await rs.refresh_from_db(db)
        assert rs.warning_days() == 42
