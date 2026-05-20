"""v4.3.7 — config_runtime: stop persisting Python None as literal "None".

Closes the smtp_to="None" finding from BUG-031's live verification on
2026-05-19. The dry-run notifier returned `recipients: ["None"]` — a
literal string — because `settings.smtp_to` had been stored in
``system_settings`` as the string ``"None"``. Root cause: ``save()``
did ``raw = str(val)``, which turns Python None into the string
``"None"``. The string then passed the downstream truthy check
``if settings.smtp_to:`` and got added to recipient sets, causing
alerts to be addressed to the literal string ``"None"`` (silently
bounced).

The fix has three parts (all in ``app/config_runtime.py``):

1. ``save()`` now stores empty string when val is None (instead of
   the literal "None").
2. ``_coerce()`` for str-typed fields treats empty string AND the
   legacy "None" sentinel as Python None (backward-compatible for
   nodes that still have pre-fix data).
3. The same None→"" mapping is applied when projecting updates into
   the live ``settings`` singleton.

These tests pin all three behaviours.
"""
from __future__ import annotations

import pytest

from app.config_runtime import _coerce


# ── _coerce ──────────────────────────────────────────────────────


def test_coerce_str_empty_becomes_none():
    assert _coerce("", "str") is None


def test_coerce_str_literal_none_becomes_none():
    """Legacy data from pre-fix nodes (raw='None') should be tolerated."""
    assert _coerce("None", "str") is None


def test_coerce_str_real_value_passes_through():
    assert _coerce("ops@example.com", "str") == "ops@example.com"


def test_coerce_str_does_not_strip_meaningful_strings():
    """A legitimate 'None'-suffixed value (unlikely but possible) is
    not stripped; only the exact literal 'None' is treated as None."""
    assert _coerce("Nones", "str") == "Nones"
    assert _coerce("none@example.com", "str") == "none@example.com"
    assert _coerce("Other-None", "str") == "Other-None"


def test_coerce_bool_unchanged():
    assert _coerce("true", "bool") is True
    assert _coerce("False", "bool") is False
    assert _coerce("", "bool") is False
    # legacy "None" string for a bool field — neither truthy literal,
    # so coerces to False (consistent with how Pydantic Field defaults work)
    assert _coerce("None", "bool") is False


def test_coerce_int_unchanged():
    assert _coerce("42", "int") == 42
    assert _coerce("0", "int") == 0


def test_coerce_float_unchanged():
    assert _coerce("3.14", "float") == 3.14


# ── save() — integration-style with an in-memory DB ──────────────


@pytest.mark.asyncio
async def test_save_writes_empty_string_for_none(monkeypatch):
    """save() must not write the literal string "None" when val is None."""
    from sqlalchemy.ext.asyncio import (
        create_async_engine, async_sessionmaker, AsyncSession,
    )
    from app.models.db import Base, SystemSetting
    from app.config_runtime import save, SCHEMA

    # Skip if smtp_to is not in SCHEMA (would mean config_runtime drifted)
    if "smtp_to" not in SCHEMA:
        pytest.skip("smtp_to not registered in SCHEMA")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession,
                                 expire_on_commit=False)

    try:
        async with Session() as db:
            await save(db, {"smtp_to": None})
            from sqlalchemy import select
            r = await db.execute(
                select(SystemSetting).where(SystemSetting.key == "smtp_to")
            )
            row = r.scalar_one()
            # The key thing: NOT the literal "None"
            assert row.value != "None", \
                f"save() persisted literal 'None' string: {row.value!r}"
            # Empty string is the expected representation of None
            assert row.value == "", \
                f"save() should write empty string for None, got {row.value!r}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_save_writes_real_string_unchanged(monkeypatch):
    """A legitimate string value still goes in as-is."""
    from sqlalchemy.ext.asyncio import (
        create_async_engine, async_sessionmaker, AsyncSession,
    )
    from app.models.db import Base, SystemSetting
    from app.config_runtime import save, SCHEMA

    if "smtp_to" not in SCHEMA:
        pytest.skip("smtp_to not registered in SCHEMA")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession,
                                 expire_on_commit=False)

    try:
        async with Session() as db:
            await save(db, {"smtp_to": "ops@example.com"})
            from sqlalchemy import select
            r = await db.execute(
                select(SystemSetting).where(SystemSetting.key == "smtp_to")
            )
            row = r.scalar_one()
            assert row.value == "ops@example.com"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_save_then_load_round_trips_none_correctly(monkeypatch):
    """save(None) → load() must yield Python None on settings.smtp_to,
    NOT the literal "None"."""
    from sqlalchemy.ext.asyncio import (
        create_async_engine, async_sessionmaker, AsyncSession,
    )
    from app.models.db import Base
    from app.config import settings as live_settings
    from app.config_runtime import save, load, SCHEMA

    if "smtp_to" not in SCHEMA:
        pytest.skip("smtp_to not registered in SCHEMA")

    # Reset live settings.smtp_to first
    monkeypatch.setattr(live_settings, "smtp_to", "starting-value",
                        raising=False)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession,
                                 expire_on_commit=False)

    try:
        async with Session() as db:
            await save(db, {"smtp_to": None})
        # save() applies to the live singleton — should be None now
        assert getattr(live_settings, "smtp_to", "sentinel") is None, \
            f"after save(None), live settings.smtp_to is {live_settings.smtp_to!r}"

        # Re-set to something else and ensure load() also yields None
        monkeypatch.setattr(live_settings, "smtp_to", "reset-value",
                            raising=False)
        async with Session() as db:
            await load(db)
        assert getattr(live_settings, "smtp_to", "sentinel") is None, \
            f"after load(), live settings.smtp_to is {live_settings.smtp_to!r}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_tolerates_legacy_none_string(monkeypatch):
    """Backward compat: a pre-fix row with value='None' loads as Python None."""
    from sqlalchemy.ext.asyncio import (
        create_async_engine, async_sessionmaker, AsyncSession,
    )
    from app.models.db import Base, SystemSetting
    from app.config import settings as live_settings
    from app.config_runtime import load, SCHEMA
    import time

    if "smtp_to" not in SCHEMA:
        pytest.skip("smtp_to not registered in SCHEMA")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession,
                                 expire_on_commit=False)

    try:
        # Directly insert the legacy row shape
        async with Session() as db:
            db.add(SystemSetting(key="smtp_to", value="None",
                                 value_type="str", updated_at=time.time()))
            await db.commit()
        monkeypatch.setattr(live_settings, "smtp_to", "sentinel",
                            raising=False)
        async with Session() as db:
            await load(db)
        assert getattr(live_settings, "smtp_to", "x") is None, \
            f"legacy 'None' string should load as None, got " \
            f"{getattr(live_settings, 'smtp_to', None)!r}"
    finally:
        await engine.dispose()
