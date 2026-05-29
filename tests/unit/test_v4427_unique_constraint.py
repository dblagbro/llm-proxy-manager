"""v4.4.27 — UNIQUE(provider_id, captured_at) on provider_ai_review
(+ mirror on api_key_ai_review).

Permanent fix for BUG-079. The v4.4.24 `.limit(1)` guard prevents the
crash on duplicates; this enforces no-duplicates-can-be-written. The
race is still live without it: between v4.4.24 (2026-05-27, de-duped
www2) and v4.4.27 prep (2026-05-28), www2 accumulated 3 NEW duplicate
groups. UNIQUE INDEX stops it at the schema level.

Migration runs in `init_db` and is idempotent (the DELETE is a no-op
once clean; CREATE UNIQUE INDEX is IF NOT EXISTS).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


# ── Source guards ───────────────────────────────────────────────────


def test_init_db_includes_unique_index_for_both_ai_review_tables():
    """The migration loop must cover BOTH tables affected by BUG-079's
    duplicate-row family — provider_ai_review (the active hit) AND
    api_key_ai_review (mirror handler, latent risk per BUG-080)."""
    src = Path("app/models/database.py").read_text()
    idx = src.index("BUG-079 permanent fix")
    block = src[idx:idx + 4000]
    assert '"provider_ai_review", ("provider_id", "captured_at")' in block, (
        "migration loop must include provider_ai_review"
    )
    assert '"api_key_ai_review", ("api_key_id", "captured_at")' in block, (
        "migration loop must include api_key_ai_review"
    )


def test_init_db_runs_dedup_before_unique_index_in_executable_code():
    """In the executable migration code (skipping comments), the DELETE
    must come BEFORE the CREATE UNIQUE INDEX — otherwise the index
    creation would fail on a DB with pre-existing duplicate rows."""
    src = Path("app/models/database.py").read_text()
    idx = src.index("BUG-079 permanent fix")
    block = src[idx:idx + 4000]
    # The SQL statements live inside `exec_driver_sql(...)` calls — match
    # those specifically, not the prose mentions in the comment block.
    delete_call_idx = block.index('exec_driver_sql(f"""\n                    DELETE FROM')
    create_call_idx = block.index('exec_driver_sql(\n                    f"CREATE UNIQUE INDEX')
    assert delete_call_idx < create_call_idx, (
        "DELETE (de-dup) must precede CREATE UNIQUE INDEX in init_db"
    )


def test_init_db_uses_lifecycle_keeper_heuristic():
    """When deduping, prefer rows carrying any non-NULL lifecycle field
    (applied_at / dismissed_at / reverted_at) — that row represents
    operator action and should win over an empty duplicate."""
    src = Path("app/models/database.py").read_text()
    idx = src.index("BUG-079 permanent fix")
    block = src[idx:idx + 4000]
    for field in ("applied_at", "dismissed_at", "reverted_at"):
        # Must appear inside the DELETE SQL's ORDER BY (the heuristic),
        # not only the docstring.
        assert f"CASE WHEN {field} IS NOT NULL THEN 0" in block, (
            f"de-dup heuristic must rank by {field} so operator-action "
            f"rows aren't discarded in favor of empty duplicates"
        )


def test_init_db_migration_is_idempotent():
    """`IF NOT EXISTS` on the CREATE INDEX + the inherent no-op of
    DELETE-on-already-deduped data make the migration safe to re-run
    on every boot."""
    src = Path("app/models/database.py").read_text()
    idx = src.index("BUG-079 permanent fix")
    block = src[idx:idx + 4000]
    # The CREATE UNIQUE INDEX line must carry IF NOT EXISTS.
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in block, (
        "CREATE UNIQUE INDEX must be IF NOT EXISTS for idempotency"
    )


# ── Behavioral tests ────────────────────────────────────────────────


@pytest_asyncio.fixture
async def fresh_db_with_migration():
    """Spin up a fresh DB and run the v4.4.27 migration paths against
    it. Returns the AsyncSessionLocal factory."""
    from app.models.database import engine, AsyncSessionLocal, init_db
    from app.models.db import Base, ProviderAiReview
    # Wipe + rebuild so we test the migration on a clean baseline.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield AsyncSessionLocal
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_unique_constraint_rejects_direct_duplicate(fresh_db_with_migration):
    """The real point of the migration: trying to INSERT a row that
    collides with an existing `(provider_id, captured_at)` must raise
    IntegrityError. Pre-v4.4.27 this succeeded silently and crashed
    apply_sync later."""
    from app.models.db import ProviderAiReview

    cap = datetime(2026, 5, 28, 12, 0, 0)
    async with fresh_db_with_migration() as db:
        db.add(ProviderAiReview(
            provider_id="prov-uc", captured_at=cap, llm_verdict="watch",
        ))
        await db.commit()

        db.add(ProviderAiReview(
            provider_id="prov-uc", captured_at=cap, llm_verdict="dismiss",
        ))
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio
async def test_unique_constraint_allows_different_captured_at(fresh_db_with_migration):
    """Sanity: a second row with a DIFFERENT captured_at (or different
    provider_id) is allowed — the constraint is on the pair, not on
    either column individually."""
    from app.models.db import ProviderAiReview

    async with fresh_db_with_migration() as db:
        db.add(ProviderAiReview(
            provider_id="prov-ok",
            captured_at=datetime(2026, 5, 28, 12, 0, 0),
            llm_verdict="watch",
        ))
        db.add(ProviderAiReview(
            provider_id="prov-ok",
            captured_at=datetime(2026, 5, 28, 12, 5, 0),  # 5 minutes later
            llm_verdict="watch",
        ))
        db.add(ProviderAiReview(
            provider_id="prov-other",
            captured_at=datetime(2026, 5, 28, 12, 0, 0),
            llm_verdict="watch",
        ))
        await db.commit()

        n = (await db.execute(select(ProviderAiReview))).all()
        assert len(n) == 3


@pytest.mark.asyncio
async def test_migration_dedups_pre_existing_duplicates():
    """Seed duplicates BEFORE the migration runs, then run the
    migration: it must de-dup such that the UNIQUE index creation
    succeeds, and the kept row is the one carrying lifecycle fields."""
    from app.models.database import engine, AsyncSessionLocal, init_db
    from app.models.db import Base, ProviderAiReview

    # Build the table WITHOUT running the v4.4.27 migration paths first.
    # Easiest: drop_all + create_all (which won't add the UNIQUE index
    # since that's only created by the init_db migration block).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    cap = datetime(2026, 5, 21, 12, 8, 9)
    async with AsyncSessionLocal() as db:
        # Two duplicates: one with no lifecycle fields, one with applied_at.
        db.add(ProviderAiReview(
            provider_id="dup-prov", captured_at=cap,
            llm_verdict="watch",
        ))
        db.add(ProviderAiReview(
            provider_id="dup-prov", captured_at=cap,
            llm_verdict="promote", applied_at=datetime(2026, 5, 22),
        ))
        await db.commit()
        # Sanity: 2 rows present
        assert len((await db.execute(select(ProviderAiReview))).all()) == 2

    # Now run init_db → it should de-dup and create the UNIQUE index.
    await init_db()

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ProviderAiReview).where(ProviderAiReview.provider_id == "dup-prov")
        )).scalars().all()
        assert len(rows) == 1, "migration should have collapsed to 1 row"
        # The keeper should be the one with applied_at (lifecycle action)
        assert rows[0].applied_at is not None, (
            "de-dup keeper heuristic should prefer the row with lifecycle "
            "fields — operator action shouldn't be lost to an empty duplicate"
        )

    # And a follow-up attempt to insert the same duplicate now raises
    async with AsyncSessionLocal() as db:
        db.add(ProviderAiReview(
            provider_id="dup-prov", captured_at=cap,
            llm_verdict="dismiss",
        ))
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio
async def test_migration_is_safe_to_rerun():
    """init_db runs on every container start. Calling it twice on an
    already-migrated DB must not raise."""
    from app.models.database import engine, init_db
    from app.models.db import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    # Second call must be a no-op (idempotent migration block)
    await init_db()
