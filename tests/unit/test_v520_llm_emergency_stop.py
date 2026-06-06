"""v5.2.0 / Batch V1 — LLM emergency stop tests.

Covers acceptance criteria:
- emergency stop blocks ALL providers (system-wide, ignores per-key policy)
- emergency stop logs/audits the block (per flip + per blocked request)
- /v1/messages + /v1/chat/completions both honor it
- background callers via acompletion_with_retry honor it
- cache invalidation on toggle is eager (no 30s wait)
- direct DB write (simulating cluster sync) propagates after cache TTL
- default OFF — pre-v5.2.0 behavior preserved
- reject zero/negative arguments isn't applicable here (boolean only)
- audit row is system-scope, never purgeable
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


@pytest.fixture(autouse=True)
def _clear_emergency_stop_cache():
    """The llm_emergency_stop module keeps a process-wide TTL cache so
    the hot path doesn't touch the DB on every call. Tests engage and
    disengage the stop using their own in-memory DBs; the cache value
    is module-global and would carry over into the next test in the
    same process, causing false-positive 503s in unrelated test files
    (notably test_v5_messages_ua_block which exercises the same
    request handler path). Wipe before AND after every test."""
    from app.monitoring import llm_emergency_stop as les
    les.invalidate_cache()
    yield
    les.invalidate_cache()


# ── Source-level pins ───────────────────────────────────────────────


def test_emergency_stop_module_exists():
    from app.monitoring import llm_emergency_stop as les
    for fn in (
        les.is_llm_stopped, les.is_llm_stopped_session_less,
        les.set_llm_stopped, les.invalidate_cache,
    ):
        assert callable(fn)
    assert les.SETTING_KEY == "compliance.llm_emergency_stop"
    assert les.REASON_CODE == "llm-emergency-stop"


def test_handler_helper_wired_into_messages():
    src = Path("app/api/messages.py").read_text()
    assert "raise_if_llm_emergency_stopped" in src
    # Must fire BEFORE provider selection: appear before
    # `select_provider_with_503` (the first router touch).
    es_idx = src.find("raise_if_llm_emergency_stopped")
    sel_idx = src.find("select_provider_with_503")
    assert es_idx != -1 and sel_idx != -1
    assert es_idx < sel_idx, (
        "Emergency stop must run BEFORE provider selection or it "
        "wastes a select_provider call on a halted fleet."
    )


def test_handler_helper_wired_into_completions():
    src = Path("app/api/completions.py").read_text()
    assert "raise_if_llm_emergency_stopped" in src
    es_idx = src.find("raise_if_llm_emergency_stopped")
    sel_idx = src.find("select_provider_with_503")
    assert es_idx != -1 and sel_idx != -1
    assert es_idx < sel_idx


def test_retry_wrapper_gates_background_callers():
    """``acompletion_with_retry`` is called by runs/worker.py,
    runs/compaction.py, cot/branches.py, cot/structured_output.py,
    routing/cascade.py. The emergency-stop check at the top of the
    wrapper covers all of them with one edit."""
    src = Path("app/routing/retry.py").read_text()
    assert "is_llm_stopped_session_less" in src
    assert "LLMEmergencyStopError" in src
    # Check fires BEFORE the retry loop (so a halt isn't burned across
    # `max_retries+1` litellm calls).
    check_idx = src.find("is_llm_stopped_session_less()")
    loop_idx = src.find("for attempt in range(")
    assert check_idx != -1 and loop_idx != -1
    assert check_idx < loop_idx


def test_admin_router_registered():
    src = Path("app/main.py").read_text()
    assert "admin_llm_emergency_router" in src
    src2 = Path("app/api/admin_llm_emergency.py").read_text()
    assert '@router.get("/status")' in src2
    assert '@router.post("/toggle")' in src2


# ── Behavioral ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_is_not_stopped():
    """Pre-feature behavior preserved: no setting row → not stopped."""
    from app.monitoring import llm_emergency_stop as les
    les.invalidate_cache()
    Session = await _fresh_db()
    async with Session() as db:
        assert await les.is_llm_stopped(db) is False


@pytest.mark.asyncio
async def test_set_then_is_stopped():
    from app.monitoring import llm_emergency_stop as les
    les.invalidate_cache()
    Session = await _fresh_db()
    async with Session() as db:
        result = await les.set_llm_stopped(
            db, enabled=True, actor="qa", reason="test",
        )
        assert result["ok"] is True
        assert result["enabled"] is True
        assert result["prior_state"] is False
        assert result["noop"] is False
        assert await les.is_llm_stopped(db) is True
        # Disengage
        result2 = await les.set_llm_stopped(
            db, enabled=False, actor="qa", reason="all-clear",
        )
        assert result2["prior_state"] is True
        assert result2["enabled"] is False
        assert await les.is_llm_stopped(db) is False


@pytest.mark.asyncio
async def test_noop_flip_still_audits():
    """Re-engaging an already-engaged stop must record an audit row —
    operator intent must be visible even on no-ops."""
    from app.monitoring import llm_emergency_stop as les
    from app.models.db import CompliancePolicyChange
    from sqlalchemy import select, func
    les.invalidate_cache()
    Session = await _fresh_db()
    async with Session() as db:
        await les.set_llm_stopped(db, enabled=True, actor="qa")
        result = await les.set_llm_stopped(db, enabled=True, actor="qa")
        assert result["noop"] is True
        # Two rows: the engage + the re-affirm
        count = (await db.execute(
            select(func.count()).select_from(CompliancePolicyChange)
        )).scalar_one()
        assert count == 2


@pytest.mark.asyncio
async def test_audit_row_fields():
    """Each flip writes a CompliancePolicyChange row with scope=system,
    actor, reason, before/after state JSON."""
    from app.monitoring import llm_emergency_stop as les
    from app.models.db import CompliancePolicyChange
    from sqlalchemy import select
    les.invalidate_cache()
    Session = await _fresh_db()
    async with Session() as db:
        await les.set_llm_stopped(
            db, enabled=True, actor="alice", reason="incident-2026-06-06",
        )
        row = (await db.execute(select(CompliancePolicyChange))).scalar_one()
        assert row.scope == "system"
        assert row.changed_by_user_id == "alice"
        assert "ENGAGED" in row.reason
        assert "incident-2026-06-06" in row.reason
        assert '"llm_emergency_stop": false' in row.before_state
        assert '"llm_emergency_stop": true' in row.after_state


@pytest.mark.asyncio
async def test_cache_invalidate_on_toggle():
    """Setting the flag must immediately reflect on the next read —
    not after the 30s TTL expires. The toggle calls invalidate_cache().
    """
    from app.monitoring import llm_emergency_stop as les
    les.invalidate_cache()
    Session = await _fresh_db()
    async with Session() as db:
        # Prime the cache with False
        assert await les.is_llm_stopped(db) is False
        # Flip
        await les.set_llm_stopped(db, enabled=True, actor="qa")
        # Immediately reads True (no sleep)
        assert await les.is_llm_stopped(db) is True


@pytest.mark.asyncio
async def test_direct_db_write_propagates_after_refresh():
    """Simulates a cluster-sync apply: a peer's value lands in
    system_settings directly. After invalidating the cache (or TTL
    expiry), the new value surfaces."""
    from app.monitoring import llm_emergency_stop as les
    from app.models.db import SystemSetting
    les.invalidate_cache()
    Session = await _fresh_db()
    async with Session() as db:
        # Prime False
        assert await les.is_llm_stopped(db) is False
        # Direct write — bypass set_llm_stopped to simulate a peer's
        # cluster_sync apply.
        db.add(SystemSetting(
            key=les.SETTING_KEY, value="true",
            value_type="bool", updated_at=0.0,
        ))
        await db.commit()
        # Cache still says False
        assert await les.is_llm_stopped(db) is False
        # Invalidate → now True
        les.invalidate_cache()
        assert await les.is_llm_stopped(db) is True


@pytest.mark.asyncio
async def test_retry_wrapper_raises_when_stopped():
    """``acompletion_with_retry`` must raise LLMEmergencyStopError when
    the stop is engaged, BEFORE attempting any litellm call."""
    from app.monitoring import llm_emergency_stop as les
    from app.routing.retry import acompletion_with_retry
    les.invalidate_cache()
    Session = await _fresh_db()
    # Patch AsyncSessionLocal to return our in-memory session so
    # is_llm_stopped_session_less() sees the engaged setting.
    import app.models.database as _dbmod
    _saved = _dbmod.AsyncSessionLocal
    _dbmod.AsyncSessionLocal = Session
    try:
        async with Session() as db:
            await les.set_llm_stopped(db, enabled=True, actor="qa")
        with pytest.raises(les.LLMEmergencyStopError):
            await acompletion_with_retry(
                model="anthropic/claude-3-5-haiku-20241022",
                messages=[{"role": "user", "content": "hi"}],
            )
    finally:
        _dbmod.AsyncSessionLocal = _saved
        les.invalidate_cache()


@pytest.mark.asyncio
async def test_emergency_stop_is_orthogonal_to_logging_stop():
    """The v5.2.0 LLM kill switch and the v5.1.0 logging kill switch
    must be independent — engaging one must NOT engage the other.
    Different SETTING_KEY guards against an inadvertent merge."""
    from app.monitoring import llm_emergency_stop as les
    from app.monitoring import logging_controls as lc
    assert les.SETTING_KEY != lc.SETTING_KEY
    les.invalidate_cache()
    lc.invalidate_cache()
    Session = await _fresh_db()
    async with Session() as db:
        # Engage LLM stop only
        await les.set_llm_stopped(db, enabled=True, actor="qa")
        assert await les.is_llm_stopped(db) is True
        assert await lc.is_logging_enabled(db) is True  # default ON


# ── End-to-end via the handler helper ──────────────────────────────


@pytest.mark.asyncio
async def test_handler_helper_no_op_when_disengaged():
    """raise_if_llm_emergency_stopped is a no-op when the stop is OFF."""
    from app.api._compliance_handler import raise_if_llm_emergency_stopped
    from app.monitoring import llm_emergency_stop as les
    les.invalidate_cache()
    Session = await _fresh_db()
    class FakeKey:
        id = "key-test-1"
    async with Session() as db:
        await raise_if_llm_emergency_stopped(
            db, FakeKey(), endpoint="messages", requested_model="claude-haiku",
        )  # no raise


@pytest.mark.asyncio
async def test_handler_helper_raises_503_when_engaged():
    """When engaged, raises HTTPException(503) with the right code +
    writes a compliance_events audit row."""
    from fastapi import HTTPException
    from app.api._compliance_handler import raise_if_llm_emergency_stopped
    from app.monitoring import llm_emergency_stop as les
    from app.models.db import ComplianceEvent, ApiKey
    from sqlalchemy import select
    les.invalidate_cache()
    Session = await _fresh_db()
    async with Session() as db:
        # ComplianceEvent has FK to api_keys; create the row first.
        db.add(ApiKey(
            id="key-test-2", key_hash="h-key-test-2",
            key_prefix="k-test-2", name="t",
        ))
        await db.commit()
        await les.set_llm_stopped(db, enabled=True, actor="qa")

        class FakeKey:
            id = "key-test-2"

        with pytest.raises(HTTPException) as exc_info:
            await raise_if_llm_emergency_stopped(
                db, FakeKey(),
                endpoint="messages",
                requested_model="claude-haiku-4-5",
            )
        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail
        assert detail["error"]["code"] == "llm-emergency-stop"
        assert detail["error"]["audit_id"]
        # Audit row written
        ev = (await db.execute(
            select(ComplianceEvent).where(
                ComplianceEvent.event_type == "llm_emergency_stop"
            )
        )).scalar_one()
        assert ev.reason_code == "llm-emergency-stop"
        assert ev.http_status == 503
        assert ev.requested_model == "claude-haiku-4-5"
