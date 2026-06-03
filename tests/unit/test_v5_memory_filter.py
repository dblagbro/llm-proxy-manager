"""v5.0.0 compliance — caller-memory ``source_company`` filtering + persistence.

Covers ``app.memory.store``:
- ``get()`` with ``blocked_companies={"anthropic"}`` returns None when the
  stored row has ``source_company="anthropic"`` (banned company).
- ``get()`` with a non-empty blocklist returns None when ``source_company``
  is NULL (decision 7 — unknown provenance is treated as banned).
- ``get()`` with an empty blocklist still returns the row (no filter).
- ``put()`` persists ``source_company`` to BOTH the ``CallerMemory`` and
  the ``CallerMemoryMarker`` rows.
- ``put()`` carrying ``source_company=None`` does not overwrite a
  previously-set value (provenance is monotone — admin writes / unknown
  providers shouldn't erase a real attribution).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.db import Base


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


# ── get() filter behavior ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_drops_row_when_source_company_banned(db):
    """A memory row tagged with a banned company must not surface — even
    if the api_key has a perfectly valid row otherwise."""
    from app.memory.store import put, get
    await put(
        db, api_key_id="k1", content="anthropic-side memo",
        conversation_id="c1", memory_tag="t1",
        source_company="anthropic",
    )
    out = await get(
        db, "k1", "c1", "t1",
        blocked_companies={"anthropic"},
    )
    assert out is None


@pytest.mark.asyncio
async def test_get_drops_row_when_source_company_null_and_blocklist_nonempty(db):
    """Decision 7 — NULL source_company is banned whenever a blocklist
    is in effect. This prevents pre-v5 memory rows (no provenance) from
    leaking to compliance-scoped keys after upgrade."""
    from app.memory.store import put, get
    await put(
        db, api_key_id="k1", content="legacy untagged memo",
        conversation_id="c1", memory_tag="t1",
        source_company=None,
    )
    out = await get(
        db, "k1", "c1", "t1",
        blocked_companies={"anthropic"},
    )
    assert out is None


@pytest.mark.asyncio
async def test_get_returns_row_when_blocklist_empty(db):
    """Empty blocklist must not filter — legacy keys with no compliance
    policy keep working identically."""
    from app.memory.store import put, get
    await put(
        db, api_key_id="k1", content="hello",
        conversation_id="c1", memory_tag="t1",
        source_company=None,
    )
    out = await get(db, "k1", "c1", "t1", blocked_companies=None)
    assert out is not None
    assert out.content == "hello"

    out = await get(db, "k1", "c1", "t1", blocked_companies=set())
    assert out is not None


@pytest.mark.asyncio
async def test_get_returns_row_when_company_not_in_blocklist(db):
    """A row whose source_company is outside the banned set should
    still be returned even with a non-empty blocklist."""
    from app.memory.store import put, get
    await put(
        db, api_key_id="k1", content="openai-side memo",
        conversation_id="c1", memory_tag="t1",
        source_company="openai",
    )
    out = await get(
        db, "k1", "c1", "t1",
        blocked_companies={"anthropic"},
    )
    assert out is not None
    assert out.source_company == "openai"


# ── put() persistence on both content + marker rows ────────────────────


@pytest.mark.asyncio
async def test_put_persists_source_company_to_caller_memory(db):
    from app.memory.store import put
    from app.models.db import CallerMemory
    await put(
        db, api_key_id="k1", content="x",
        conversation_id="c1", memory_tag="t1",
        source_company="anthropic",
    )
    row = (await db.execute(
        select(CallerMemory).where(CallerMemory.api_key_id == "k1")
    )).scalar_one()
    assert row.source_company == "anthropic"


@pytest.mark.asyncio
async def test_put_persists_source_company_to_marker(db):
    from app.memory.store import put
    from app.models.db import CallerMemoryMarker
    await put(
        db, api_key_id="k1", content="x",
        conversation_id="c1", memory_tag="t1",
        source_company="openai",
    )
    marker = (await db.execute(
        select(CallerMemoryMarker).where(CallerMemoryMarker.api_key_id == "k1")
    )).scalar_one()
    assert marker.source_company == "openai"


@pytest.mark.asyncio
async def test_put_existing_row_with_none_does_not_clobber_company(db):
    """Provenance is monotone — once a row has a known company, a
    later admin write with source_company=None must NOT erase it.
    Otherwise admin-side edits would silently downgrade a known-good
    attribution to NULL (= banned under decision 7)."""
    from app.memory.store import put
    from app.models.db import CallerMemory
    await put(
        db, api_key_id="k1", content="first",
        conversation_id="c1", memory_tag="t1",
        source_company="anthropic",
    )
    await put(
        db, api_key_id="k1", content="second-via-admin",
        conversation_id="c1", memory_tag="t1",
        source_company=None,
    )
    row = (await db.execute(
        select(CallerMemory).where(CallerMemory.api_key_id == "k1")
    )).scalar_one()
    assert row.content == "second-via-admin"
    assert row.source_company == "anthropic", (
        "source_company must not be erased by a later put() with None"
    )


@pytest.mark.asyncio
async def test_put_new_row_with_none_company_persists_null(db):
    """A fresh row with no provenance keeps source_company=NULL — only
    the OVERWRITE path is monotone."""
    from app.memory.store import put
    from app.models.db import CallerMemory
    await put(
        db, api_key_id="k1", content="x",
        conversation_id="c1", memory_tag="t1",
    )
    row = (await db.execute(
        select(CallerMemory).where(CallerMemory.api_key_id == "k1")
    )).scalar_one()
    assert row.source_company is None


# ── MemoryEntry dataclass surface ─────────────────────────────────────


def test_memory_entry_has_source_company_field():
    from dataclasses import fields
    from app.memory.store import MemoryEntry
    names = {f.name for f in fields(MemoryEntry)}
    assert "source_company" in names
