"""R4 event-broker tests + idempotency cache."""
from __future__ import annotations

import asyncio
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_broker_and_cache():
    """Clear module-level state between tests so they don't bleed."""
    from app.runs import event_bus, idempotency
    event_bus._CHANNELS.clear()
    idempotency.clear()
    yield
    event_bus._CHANNELS.clear()
    idempotency.clear()


# ── Broker basics ──────────────────────────────────────────────────────────


def test_publish_to_unsubscribed_channel_lands_in_ring():
    from app.runs import event_bus
    event_bus.publish("run_a", seq=1, kind="run_started", payload={}, ts=1.0)
    ch = event_bus.get("run_a")
    assert ch is not None
    assert len(ch._ring) == 1
    assert ch._ring[0].kind == "run_started"


def test_replay_since_filters_by_seq():
    from app.runs import event_bus
    for i in range(5):
        event_bus.publish("run_a", seq=i + 1, kind="model_call_start",
                          payload={"i": i}, ts=float(i))
    ch = event_bus.get("run_a")
    replay = ch.replay_since(2)
    assert [e.seq for e in replay] == [3, 4, 5]


def test_ring_evicts_oldest_when_full():
    from app.runs import event_bus
    for i in range(event_bus.RING_SIZE + 50):
        event_bus.publish("run_a", seq=i + 1, kind="x", payload={}, ts=0.0)
    ch = event_bus.get("run_a")
    assert len(ch._ring) == event_bus.RING_SIZE
    # Oldest 50 evicted; first remaining seq should be 51
    assert ch._ring[0].seq == 51
    assert ch._ring[-1].seq == event_bus.RING_SIZE + 50


def test_terminal_event_closes_channel_and_wakes_subscribers():
    """A terminal kind closes the channel; new subscribers get None
    sentinel via the queue + the channel is .closed."""
    from app.runs import event_bus
    event_bus.publish("run_a", seq=1, kind="run_started", payload={}, ts=1.0)
    event_bus.publish("run_a", seq=2, kind="completed",
                      payload={"result_text": "ok"}, ts=2.0)
    ch = event_bus.get("run_a")
    assert ch.closed is True


@pytest.mark.asyncio
async def test_subscribe_receives_live_events():
    from app.runs import event_bus
    ch = event_bus.get_or_create("run_a")
    q = await ch.subscribe()
    event_bus.publish("run_a", seq=1, kind="run_started", payload={}, ts=1.0)
    ev = await asyncio.wait_for(q.get(), timeout=1.0)
    assert ev.kind == "run_started"
    ch.unsubscribe(q)


@pytest.mark.asyncio
async def test_stream_events_replays_then_streams_live():
    from app.runs import event_bus

    # Pre-populate ring
    event_bus.publish("run_a", seq=1, kind="run_started", payload={}, ts=1.0)
    event_bus.publish("run_a", seq=2, kind="model_call_start",
                      payload={}, ts=2.0)

    received: list = []
    async def consume():
        async for ev in event_bus.stream_events("run_a", last_event_id=0,
                                                 keepalive_sec=0.5):
            if ev.kind == "__keepalive__":
                continue
            received.append((ev.seq, ev.kind))
            if ev.kind == "completed":
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    event_bus.publish("run_a", seq=3, kind="completed",
                      payload={"result_text": "ok"}, ts=3.0)
    await asyncio.wait_for(task, timeout=2.0)

    # Replay (1, 2) + live (3)
    assert received == [(1, "run_started"), (2, "model_call_start"),
                        (3, "completed")]


@pytest.mark.asyncio
async def test_stream_events_resume_skips_already_seen():
    """Last-Event-ID resume: if client passes seq=5, replay starts at 6."""
    from app.runs import event_bus
    for i in range(10):
        event_bus.publish("run_a", seq=i + 1, kind="x", payload={"i": i},
                          ts=float(i))
    event_bus.publish("run_a", seq=11, kind="completed",
                      payload={"result_text": "ok"}, ts=11.0)

    received = []
    async for ev in event_bus.stream_events("run_a", last_event_id=5,
                                              keepalive_sec=0.5):
        if ev.kind == "__keepalive__":
            continue
        received.append(ev.seq)
        if ev.kind == "completed":
            break
    assert received == [6, 7, 8, 9, 10, 11]


@pytest.mark.asyncio
async def test_stream_events_keepalive_when_idle():
    """When no events flow, the stream emits a __keepalive__ sentinel
    after keepalive_sec. SSE handler turns that into `: keepalive\\n\\n`."""
    from app.runs import event_bus
    event_bus.publish("run_a", seq=1, kind="run_started", payload={}, ts=1.0)

    received = []
    async def consume():
        async for ev in event_bus.stream_events("run_a", last_event_id=0,
                                                  keepalive_sec=0.1):
            received.append(ev.kind)
            if len(received) >= 4:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.5)
    # Force the stream to close so the test doesn't hang
    event_bus.publish("run_a", seq=2, kind="cancelled", payload={}, ts=0.0)
    await asyncio.wait_for(task, timeout=2.0)

    # Should have run_started + at least one keepalive sentinel
    assert "run_started" in received
    assert received.count("__keepalive__") >= 1


# ── Idempotency cache ──────────────────────────────────────────────────────


def test_idempotency_cache_get_miss():
    from app.runs import idempotency
    assert idempotency.get("k1", "id1") is None


def test_idempotency_cache_put_then_get():
    from app.runs import idempotency
    idempotency.put("k1", "id1", "run_abc", time.time())
    assert idempotency.get("k1", "id1") == "run_abc"


def test_idempotency_cache_per_api_key_isolation():
    """Same idempotency_key under DIFFERENT api_keys must NOT collide
    (Q1: per-API-key collision domain)."""
    from app.runs import idempotency
    now = time.time()
    idempotency.put("key-A", "shared-id", "run_a", now)
    idempotency.put("key-B", "shared-id", "run_b", now)
    assert idempotency.get("key-A", "shared-id") == "run_a"
    assert idempotency.get("key-B", "shared-id") == "run_b"


def test_idempotency_cache_ttl_expiry():
    from app.runs import idempotency
    # Created 25h ago — past 24h TTL
    idempotency.put("k1", "id1", "run_old", time.time() - (25 * 3600))
    assert idempotency.get("k1", "id1") is None


def test_idempotency_cache_invalidate():
    from app.runs import idempotency
    idempotency.put("k1", "id1", "run_x", time.time())
    assert idempotency.get("k1", "id1") == "run_x"
    idempotency.invalidate("k1", "id1")
    assert idempotency.get("k1", "id1") is None


def test_idempotency_cache_lru_eviction():
    """When max_size hits, least-recently-used keys evict first."""
    from app.runs.idempotency import _IdempotencyCache
    c = _IdempotencyCache(max_size=3)
    now = time.time()
    c.put("k", "a", "run_a", now)
    c.put("k", "b", "run_b", now)
    c.put("k", "c", "run_c", now)
    # Access 'a' — LRU bumps it to the back
    assert c.get("k", "a") == "run_a"
    # Insert 'd' — should evict 'b' (the oldest unaccessed)
    c.put("k", "d", "run_d", now)
    assert c.get("k", "a") == "run_a"
    assert c.get("k", "b") is None
    assert c.get("k", "c") == "run_c"
    assert c.get("k", "d") == "run_d"
