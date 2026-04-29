"""R6 chaos + concurrency tests.

Builds on the R2 monkeypatch pattern: select_provider + acompletion are
mocked so we exercise the worker's decision logic deterministically.

Cases:
  - Per-Run rate limiter throttles + emits rate_limited event
  - Malformed JSON response (missing choices) doesn't crash
  - 100 concurrent runs survive without deadlock or memory blowup
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
async def _clean():
    from app.models.database import init_db, AsyncSessionLocal
    from sqlalchemy import text
    from app.runs import event_bus, idempotency, replication, rate_limit
    await init_db()
    async with AsyncSessionLocal() as db:
        for tbl in ("run_events", "run_messages", "run_idempotency", "runs"):
            try:
                await db.execute(text(f"DELETE FROM {tbl}"))
            except Exception:
                pass
        await db.commit()
    event_bus._CHANNELS.clear()
    idempotency.clear()
    replication._pending.clear()
    rate_limit._buckets.clear()
    yield


def _route(provider_id: str, model: str = "mock/test"):
    prov = MagicMock()
    prov.id = provider_id
    prov.name = f"prov-{provider_id}"
    prov.timeout_sec = 30
    prov.api_key = "mock-key"
    prov.base_url = None
    prov.provider_type = "openai"
    route = MagicMock()
    route.provider = prov
    route.litellm_model = model
    route.litellm_kwargs = {}
    route.profile = MagicMock(native_tools=True)
    return route


def _resp(text="ok"):
    msg = MagicMock()
    msg.content = text
    msg.tool_calls = []
    choice = MagicMock()
    choice.message = msg
    r = MagicMock()
    r.choices = [choice]
    r.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    r.model_dump = lambda: {"choices": [{"message": {"content": text}}]}
    return r


# ── Rate limiter ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_under_limit_no_throttle():
    """Under the limit, acquire returns immediately."""
    from app.runs import rate_limit
    rate_limit._buckets.clear()
    res = await rate_limit.acquire("run_a")
    assert res["attempt"] == 0
    assert res["wait_ms"] == 0


@pytest.mark.asyncio
async def test_rate_limit_throttles_on_burst(monkeypatch):
    """Exceeding the limit triggers backoff + emits rate_limited."""
    from app.runs import rate_limit
    monkeypatch.setattr("app.config.settings.runs_max_model_calls_per_minute",
                        2, raising=False)
    rate_limit._buckets.clear()

    emitted = []
    async def emit(payload):
        emitted.append(payload)

    # Two calls fit
    await rate_limit.acquire("run_b", emit_callback=emit)
    await rate_limit.acquire("run_b", emit_callback=emit)
    # Third call must wait — but we don't want to actually sleep 60s in
    # tests. Patch asyncio.sleep to a no-op + advance time so the bucket
    # sees the slot free up.
    real_sleep = asyncio.sleep
    async def fast_sleep(_d):
        # Make the oldest timestamp age out so the next iteration succeeds
        for ts_list in rate_limit._buckets.values():
            if ts_list._starts:
                ts_list._starts[0] -= 100  # slide oldest out of window
        await real_sleep(0)
    monkeypatch.setattr(rate_limit.asyncio, "sleep", fast_sleep)

    res = await rate_limit.acquire("run_b", emit_callback=emit)
    assert res["attempt"] >= 1
    assert res["wait_ms"] > 0
    assert len(emitted) == 1
    assert emitted[0]["limit_rpm"] == 2
    assert emitted[0]["current_rpm"] == 2
    assert "retry_after_ms" in emitted[0]


@pytest.mark.asyncio
async def test_rate_limit_isolation_per_run():
    from app.runs import rate_limit
    rate_limit._buckets.clear()
    await rate_limit.acquire("run_x")
    await rate_limit.acquire("run_x")
    # run_y should be unaffected
    res = await rate_limit.acquire("run_y")
    assert res["attempt"] == 0
    assert res["wait_ms"] == 0


@pytest.mark.asyncio
async def test_rate_limit_reset_drops_bucket():
    from app.runs import rate_limit
    rate_limit._buckets.clear()
    await rate_limit.acquire("run_z")
    assert "run_z" in rate_limit._buckets
    rate_limit.reset("run_z")
    assert "run_z" not in rate_limit._buckets


# ── Malformed upstream response ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_response_no_choices_treated_as_text():
    """Worker's _extract_assistant_content tolerates missing fields —
    returns an empty-text block rather than crashing."""
    from app.runs.worker import _extract_assistant_content
    # Empty resp → {choices: []}
    r = MagicMock()
    r.choices = []
    blocks = _extract_assistant_content(r)
    assert blocks == [{"type": "text", "text": ""}]


@pytest.mark.asyncio
async def test_malformed_response_missing_choices_index():
    """Worker tolerates an upstream that returns no choices array."""
    from app.runs.worker import _extract_assistant_content

    class BadResp:
        choices = None  # IndexError when accessed via [0]
    blocks = _extract_assistant_content(BadResp())
    assert blocks == [{"type": "text", "text": ""}]


# ── 100 concurrent runs ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_100_concurrent_runs_no_deadlock(monkeypatch):
    """Spawn 100 _drive() loops concurrently; each completes within ~5s.
    Asserts: no event-bus memory blowup (ring bounded), no rate_limit
    contention across runs (per-run buckets), no deadline drift."""
    from app.runs import worker as run_worker, event_bus, rate_limit
    from app.models.database import init_db, AsyncSessionLocal
    from app.models.db import Run, RunMessage
    from app.runs.ids import new_run_id
    from app.runs.state import RunStatus
    from sqlalchemy import select

    monkeypatch.setattr("app.config.settings.runs_max_model_calls_per_minute",
                        100, raising=False)
    await init_db()

    # Mock: every model call returns a text response
    async def fake_acompletion(*args, **kwargs):
        await asyncio.sleep(0.01)  # sub-realistic upstream latency
        return _resp("done")
    monkeypatch.setattr(run_worker, "acompletion_with_retry", fake_acompletion)

    async def fake_select_provider(db, hint=None, **kwargs):
        return _route("prov-load")
    monkeypatch.setattr(run_worker, "select_provider", fake_select_provider)

    # Seed 100 queued runs
    now = time.time()
    run_ids = []
    async with AsyncSessionLocal() as db:
        for i in range(100):
            rid = new_run_id()
            run_ids.append(rid)
            db.add(Run(
                id=rid, api_key_id=f"k{i % 10}", owner_node_id="local",
                status=RunStatus.QUEUED.value, deadline_ts=now + 30,
                max_turns=5, model_preference=[], system_prompt=None,
                tools_spec=[], metadata_json={},
                model_calls=0, tool_calls=0, tokens_in=0, tokens_out=0,
                created_at=now, updated_at=now,
            ))
            db.add(RunMessage(run_id=rid, seq=1, role="user",
                              content="hi", tokens=0, created_at=now))
        await db.commit()

    # Drive all 100 concurrently with a 10s ceiling
    t0 = time.monotonic()
    await asyncio.wait_for(
        asyncio.gather(*(run_worker._drive(rid) for rid in run_ids)),
        timeout=15.0,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 10.0, f"100 concurrent runs took {elapsed:.1f}s (deadlock risk)"

    # All terminal
    async with AsyncSessionLocal() as db:
        terminal_count = 0
        for rid in run_ids:
            r = (await db.execute(select(Run).where(Run.id == rid))).scalar_one()
            if r.status == "completed":
                terminal_count += 1
    assert terminal_count == 100, f"only {terminal_count}/100 completed"

    # Event-bus memory bounded — each channel ring ≤ 1000
    for ch in event_bus._CHANNELS.values():
        assert len(ch._ring) <= event_bus.RING_SIZE

    # Rate-limit buckets cleaned up on terminal
    for rid in run_ids:
        assert rid not in rate_limit._buckets, f"bucket leaked for {rid}"
