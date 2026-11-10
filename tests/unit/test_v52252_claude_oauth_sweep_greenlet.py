"""v5.22.52 / v5.22.53 — claude_oauth_expiry sweep must survive a mid-loop
rollback and keep processing the remaining providers.

The sweep calls `refresh_and_persist()` per provider; a dead refresh token
(`invalid_grant`) takes the `await db.rollback()` branch. `rollback()` ALWAYS
expires every ORM instance in the session (independent of the sessionmaker's
`expire_on_commit=False`). The sweep iterated a pre-loaded list of ORM
objects, so the NEXT provider's attribute access after that rollback
lazy-reloaded and raised `sqlalchemy.exc.MissingGreenlet: greenlet_spawn has
not been called` — killing the sweep. (v5.22.52 snapshotted id/name for the
current row but not the next iteration; v5.22.53 re-fetches each provider
fresh, which is the real fix.)

Observed live 2026-11-01: Anthropic-Gmail (invalid_grant) → `refresh_failed`
→ rollback → next provider → `sweep_failed err=...MissingGreenlet...`.
"""
from __future__ import annotations

import time

import pytest
import pytest_asyncio
from sqlalchemy import delete

_FAIL_ID = "greenlet-claude-A"   # processed first, fails → rollback
_OK_ID = "greenlet-claude-B"     # must still be processed after the rollback


@pytest_asyncio.fixture
async def fixture_db():
    from app.models.database import AsyncSessionLocal, engine
    from app.models.db import Base, Provider

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(Provider).where(Provider.id.in_([_FAIL_ID, _OK_ID])))
        await cleanup.commit()
    async with AsyncSessionLocal() as db:
        for pid, name in ((_FAIL_ID, "claude-A"), (_OK_ID, "claude-B")):
            db.add(Provider(
                id=pid, name=name, provider_type="claude-oauth",
                priority=10, enabled=True,
                oauth_refresh_token="rt-stub", oauth_expires_at=time.time() - 3600,
            ))
        await db.commit()

    yield AsyncSessionLocal

    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(Provider).where(Provider.id.in_([_FAIL_ID, _OK_ID])))
        await cleanup.commit()


@pytest.mark.asyncio
async def test_sweep_survives_rollback_and_continues(fixture_db, monkeypatch):
    import app.monitoring.claude_oauth_expiry_monitor as mon

    async def _fake_refresh_and_persist(provider, db):
        # A fails like a dead token; B "succeeds". Both touch the DB the way
        # the real call does (commit on success) so expiry semantics match.
        if provider.id == _FAIL_ID:
            raise RuntimeError("invalid_grant (simulated)")
        await db.commit()

    async def _noop_force_close(pid):
        return None

    def _noop_clear(pid):
        return None

    monkeypatch.setattr(
        "app.providers.claude_oauth_flow.refresh_and_persist", _fake_refresh_and_persist
    )
    monkeypatch.setattr("app.routing.circuit_breaker.force_close", _noop_force_close)
    monkeypatch.setattr("app.routing.circuit_breaker.clear_auth_failure", _noop_clear)

    # Pre-fix: after A's rollback, touching B lazy-reloaded → MissingGreenlet.
    await mon._run_one_sweep()

    sweep = mon.get_last_sweep()
    assert sweep["checked"] == 2, "both seeded claude-oauth providers must be checked"
    assert sweep["failed"] >= 1 and sweep["refreshed"] >= 1, "A fails, B succeeds"
    names = {d.get("name") for d in sweep["detail"]}
    assert {"claude-A", "claude-B"} <= names, "both rows recorded — the loop did not die mid-way"
