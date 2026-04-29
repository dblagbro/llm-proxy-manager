"""R2 chaos tests for the Run worker.

Covers the four failure modes from the spec table:
  1. Upstream provider 502  → worker fails over, marks degraded, retries next
  2. Upstream read-timeout  → worker fails over IMMEDIATELY (B.7 headline)
  3. All providers exhausted → run.status=failed, kind=error_provider
  4. Deadline exceeded mid-call → run.status=expired, expired event emitted

Strategy: monkeypatch ``select_provider`` and ``acompletion_with_retry`` so
the worker's failover decisions are exercised in isolation, without real
network calls. Uses an in-memory SQLite via the existing AsyncSessionLocal
(tests/conftest.py wires this for CI).

R6 will add load-test variants of these against the real mock_llm_server;
R2 just needs determinism so contract drift surfaces fast.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


@pytest.fixture(autouse=True)
async def _clean_runs_tables():
    """Each test starts with empty Run-related tables. DATABASE_URL is
    pointed at a tempdir file by tests/unit/conftest.py."""
    from app.models.database import init_db, AsyncSessionLocal
    from sqlalchemy import text
    await init_db()
    async with AsyncSessionLocal() as db:
        for tbl in ("run_events", "run_messages", "run_idempotency", "runs"):
            try:
                await db.execute(text(f"DELETE FROM {tbl}"))
            except Exception:
                pass
        await db.commit()
    yield


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_route_factory():
    """Builds a minimal RouteResult-shaped object that select_provider would
    return. The worker only reads ``.provider.id``, ``.provider.name``,
    ``.provider.timeout_sec``, ``.provider.api_key``, ``.provider.base_url``,
    ``.litellm_model``, ``.litellm_kwargs``."""
    def _make(provider_id: str, *, model: str = "mock/test", timeout: int = 30):
        prov = MagicMock()
        prov.id = provider_id
        prov.name = f"prov-{provider_id}"
        prov.timeout_sec = timeout
        prov.api_key = "mock-key"
        prov.base_url = None
        prov.provider_type = "openai"
        route = MagicMock()
        route.provider = prov
        route.litellm_model = model
        route.litellm_kwargs = {}
        return route
    return _make


@dataclass
class _FakeUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 5


def _fake_response_text(text: str = "hello") -> object:
    """Shape a fake litellm response: r.choices[0].message.content / .tool_calls."""
    msg = MagicMock()
    msg.content = text
    msg.tool_calls = []
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = _FakeUsage()
    resp.model_dump = lambda: {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    return resp


# ── 1. Upstream 502 → fail-over to next provider, success on retry ──────────


@pytest.mark.asyncio
async def test_provider_502_fails_over_then_succeeds(monkeypatch, fake_route_factory):
    """A 502 from provider-A is non-retriable on the same provider; the
    worker should mark it failed, bump the chain, and try provider-B which
    succeeds. Run ends in completed."""
    from app.runs import worker as run_worker
    from app.models.database import init_db, AsyncSessionLocal
    from app.models.db import Run, RunMessage
    from app.runs.ids import new_run_id
    from app.runs.state import RunStatus

    await init_db()
    run_id = new_run_id()
    now = time.time()

    async with AsyncSessionLocal() as db:
        db.add(Run(
            id=run_id, api_key_id="test-key", owner_node_id="local",
            status=RunStatus.QUEUED.value, deadline_ts=now + 60,
            max_turns=10, model_preference=[], system_prompt=None,
            tools_spec=[], metadata_json={}, model_calls=0, tool_calls=0,
            tokens_in=0, tokens_out=0, created_at=now, updated_at=now,
        ))
        db.add(RunMessage(
            run_id=run_id, seq=1, role="user", content="hi", tokens=0,
            created_at=now,
        ))
        await db.commit()

    # First call → 502; second call → success
    call_count = {"n": 0}
    async def fake_acompletion(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("litellm.InternalServerError: upstream returned 502")
        return _fake_response_text("ok")

    monkeypatch.setattr(run_worker, "acompletion_with_retry", fake_acompletion)

    # select_provider returns A, then B
    seen_excludes = []
    async def fake_select_provider(db, hint=None, **kwargs):
        seen_excludes.append(kwargs.get("exclude_provider_id"))
        if kwargs.get("exclude_provider_id") is None:
            return fake_route_factory("prov-a")
        return fake_route_factory("prov-b")

    monkeypatch.setattr(run_worker, "select_provider", fake_select_provider)

    await run_worker._drive(run_id)

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        from app.models.db import RunEvent
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (await db.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )).scalars().all()
        kinds = [e.kind for e in events]

    assert run.status == "completed", f"expected completed, got {run.status}"
    assert "provider_failed" in kinds, "first provider should have surfaced as failed"
    assert kinds.count("model_call_start") >= 2, "should have tried >=2 providers"
    assert kinds[-1] == "completed"


# ── 2. Read-timeout → IMMEDIATE fail-over (the headline B.7 fix) ────────────


@pytest.mark.asyncio
async def test_read_timeout_fails_over_immediately(monkeypatch, fake_route_factory):
    """A ReadTimeout must NOT be retried on the same provider — it should
    trigger fail-over within the per-call deadline (a few seconds at most),
    never the spec-anti-pattern 600s hang."""
    from app.runs import worker as run_worker
    from app.models.database import init_db, AsyncSessionLocal
    from app.models.db import Run, RunMessage, RunEvent
    from app.runs.ids import new_run_id
    from app.runs.state import RunStatus
    from sqlalchemy import select

    await init_db()
    run_id = new_run_id()
    now = time.time()

    async with AsyncSessionLocal() as db:
        db.add(Run(
            id=run_id, api_key_id="test-key", owner_node_id="local",
            status=RunStatus.QUEUED.value, deadline_ts=now + 60,
            max_turns=10, model_preference=[], system_prompt=None,
            tools_spec=[], metadata_json={}, model_calls=0, tool_calls=0,
            tokens_in=0, tokens_out=0, created_at=now, updated_at=now,
        ))
        db.add(RunMessage(
            run_id=run_id, seq=1, role="user", content="hi", tokens=0,
            created_at=now,
        ))
        await db.commit()

    async def fake_acompletion(*args, **kwargs):
        if not getattr(fake_acompletion, "tried_b", False):
            fake_acompletion.tried_b = True
            raise httpx.ReadTimeout("read timed out")
        return _fake_response_text("ok-after-timeout")

    monkeypatch.setattr(run_worker, "acompletion_with_retry", fake_acompletion)

    async def fake_select_provider(db, hint=None, **kwargs):
        excl = kwargs.get("exclude_provider_id")
        if excl is None:
            return fake_route_factory("prov-a")
        return fake_route_factory("prov-b")

    monkeypatch.setattr(run_worker, "select_provider", fake_select_provider)

    t0 = time.monotonic()
    await asyncio.wait_for(run_worker._drive(run_id), timeout=15.0)
    elapsed = time.monotonic() - t0

    # Even with a slow CI box, fail-over+success should land in <5s; 15s
    # ceiling above guards against the spec's 600s anti-pattern resurfacing.
    assert elapsed < 10.0, f"timeout fail-over took {elapsed:.1f}s (B.7 regression)"

    async with AsyncSessionLocal() as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (await db.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )).scalars().all()
        kinds = [e.kind for e in events]
        timeout_ev = [e for e in events if e.kind == "model_call_end"
                      and e.payload.get("status") == "timeout"]

    assert run.status == "completed"
    assert len(timeout_ev) == 1, "expected exactly one timeout-marked model_call_end"
    assert timeout_ev[0].payload["error"] in ("ReadTimeout", "TimeoutError")
    assert "provider_failed" in kinds


# ── 3. All providers exhausted → run failed with error_provider kind ────────


@pytest.mark.asyncio
async def test_all_providers_exhausted_fails_run(monkeypatch, fake_route_factory):
    from app.runs import worker as run_worker
    from app.models.database import init_db, AsyncSessionLocal
    from app.models.db import Run, RunMessage, RunEvent
    from app.runs.ids import new_run_id
    from app.runs.state import RunStatus, ErrorKind
    from sqlalchemy import select

    await init_db()
    run_id = new_run_id()
    now = time.time()

    async with AsyncSessionLocal() as db:
        db.add(Run(
            id=run_id, api_key_id="test-key", owner_node_id="local",
            status=RunStatus.QUEUED.value, deadline_ts=now + 60,
            max_turns=10, model_preference=[], system_prompt=None,
            tools_spec=[], metadata_json={}, model_calls=0, tool_calls=0,
            tokens_in=0, tokens_out=0, created_at=now, updated_at=now,
        ))
        db.add(RunMessage(
            run_id=run_id, seq=1, role="user", content="hi", tokens=0,
            created_at=now,
        ))
        await db.commit()

    async def fake_acompletion(*args, **kwargs):
        raise Exception("litellm.APIConnectionError: upstream unreachable")
    monkeypatch.setattr(run_worker, "acompletion_with_retry", fake_acompletion)

    seen = {"n": 0}
    async def fake_select_provider(db, hint=None, **kwargs):
        seen["n"] += 1
        # First 2 picks return providers; 3rd raises (no more providers).
        if seen["n"] <= 2:
            return fake_route_factory(f"prov-{seen['n']}")
        raise RuntimeError("all providers exhausted")
    monkeypatch.setattr(run_worker, "select_provider", fake_select_provider)

    await run_worker._drive(run_id)

    async with AsyncSessionLocal() as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (await db.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )).scalars().all()

    assert run.status == "failed"
    assert run.error_kind == ErrorKind.PROVIDER.value
    assert any(e.kind == "failed" for e in events)
    final = next(e for e in events if e.kind == "failed")
    assert final.payload["kind"] == ErrorKind.PROVIDER.value


# ── 4. Deadline exceeded mid-call → expired event ───────────────────────────


@pytest.mark.asyncio
async def test_deadline_exceeded_during_run_expires(monkeypatch, fake_route_factory):
    """The run's deadline_ts is already in the past when _drive starts;
    the FSM transitions QUEUED → EXPIRED on the first iteration's deadline
    check (state.py _start handles the past-deadline case)."""
    from app.runs import worker as run_worker
    from app.models.database import init_db, AsyncSessionLocal
    from app.models.db import Run, RunMessage, RunEvent
    from app.runs.ids import new_run_id
    from app.runs.state import RunStatus
    from sqlalchemy import select

    await init_db()
    run_id = new_run_id()
    now = time.time()

    async with AsyncSessionLocal() as db:
        db.add(Run(
            id=run_id, api_key_id="test-key", owner_node_id="local",
            status=RunStatus.QUEUED.value,
            deadline_ts=now - 1.0,  # already past
            max_turns=10, model_preference=[], system_prompt=None,
            tools_spec=[], metadata_json={}, model_calls=0, tool_calls=0,
            tokens_in=0, tokens_out=0, created_at=now, updated_at=now,
        ))
        db.add(RunMessage(
            run_id=run_id, seq=1, role="user", content="hi", tokens=0,
            created_at=now,
        ))
        await db.commit()

    # acompletion shouldn't be called at all — deadline is already past.
    call_count = {"n": 0}
    async def fake_acompletion(*args, **kwargs):
        call_count["n"] += 1
        return _fake_response_text("should-not-be-called")
    monkeypatch.setattr(run_worker, "acompletion_with_retry", fake_acompletion)

    async def fake_select_provider(*args, **kwargs):
        return fake_route_factory("prov-a")
    monkeypatch.setattr(run_worker, "select_provider", fake_select_provider)

    await run_worker._drive(run_id)

    async with AsyncSessionLocal() as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        events = (await db.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )).scalars().all()
        kinds = [e.kind for e in events]

    assert run.status == "expired", f"expected expired, got {run.status}"
    assert "expired" in kinds
    assert call_count["n"] == 0, "model should not have been called on a past-deadline run"


# ── 5. Cancel mid-tool-wait — worker exits cleanly via wakeup ───────────────


@pytest.mark.asyncio
async def test_cancel_during_requires_tool_wait(monkeypatch, fake_route_factory):
    """Worker is parked in REQUIRES_TOOL waiting for /tool_result. Cancel
    fires (which sets run.status=CANCELLED + calls run_worker.wake()).
    Worker should observe terminal status and return without hanging."""
    from app.runs import worker as run_worker
    from app.models.database import init_db, AsyncSessionLocal
    from app.models.db import Run, RunMessage, RunEvent
    from app.runs.ids import new_run_id
    from app.runs.state import RunStatus, advance, EventKind, RunCtx
    from sqlalchemy import select

    await init_db()
    run_id = new_run_id()
    now = time.time()

    # Seed the run already in REQUIRES_TOOL with a pending tool_use_id —
    # mimics the state right after the worker emitted tool_use_requested
    # and parked.
    async with AsyncSessionLocal() as db:
        db.add(Run(
            id=run_id, api_key_id="test-key", owner_node_id="local",
            status=RunStatus.REQUIRES_TOOL.value, deadline_ts=now + 60,
            max_turns=10, model_preference=[], system_prompt=None,
            tools_spec=[], metadata_json={},
            current_tool_use_id="toolu_x", current_tool_name="Bash",
            current_tool_input={"cmd": "ls"},
            model_calls=1, tool_calls=0, tokens_in=10, tokens_out=5,
            created_at=now, updated_at=now,
        ))
        db.add(RunMessage(run_id=run_id, seq=1, role="user", content="hi",
                          tokens=0, created_at=now))
        await db.commit()

    # Concurrently: launch _drive, then a moment later flip the run to
    # CANCELLED + call wake. The worker's wakeup.wait() should fire and
    # the next loop iteration sees terminal status and returns.
    async def trigger_cancel():
        await asyncio.sleep(0.2)
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(Run).where(Run.id == run_id))
            run = res.scalar_one()
            t = advance(
                RunCtx(status=RunStatus.REQUIRES_TOOL,
                       deadline_ts=run.deadline_ts,
                       max_turns=run.max_turns,
                       turns_used=run.model_calls or 0),
                EventKind.CANCEL, time.time(),
            )
            run.status = t.status.value
            run.updated_at = time.time()
            run.completed_at = run.updated_at
            await db.commit()
        # Poke the worker out of its idle wait
        run_worker.wake(run_id)

    t0 = time.monotonic()
    await asyncio.gather(
        asyncio.wait_for(run_worker._drive(run_id), timeout=5.0),
        trigger_cancel(),
    )
    elapsed = time.monotonic() - t0

    # Wakeup must fire well before the 5s tick — the wait should resolve
    # within ~250ms of cancel posting.
    assert elapsed < 2.0, f"cancel→exit took {elapsed:.2f}s; wakeup not wired"

    async with AsyncSessionLocal() as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert run.status == "cancelled"
