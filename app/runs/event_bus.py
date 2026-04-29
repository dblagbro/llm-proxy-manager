"""In-memory event broker for the Run runtime (R4).

One ``RunChannel`` per active run. Workers publish events; SSE clients
subscribe. Each channel keeps a 1000-event ring buffer so clients can
resume via ``Last-Event-ID`` after a brief disconnect without hitting the
DB. The DB still receives every event (durability for cluster handoff
in R5 and for ``GET /events?since_ms=`` after the channel is gone).

Why in-memory: SSE delivery latency target is <100ms; DB-poll loops in R1
were 1s. Multi-subscriber on a single run (e.g., the hub UI + a daemon
both watching) gets a fan-out broker for free, and the broker scope is
exactly per-run so memory cost is bounded by ``runs * 1000``.

Lifecycle:
  - Channel created lazily on first publish or first subscribe
  - Channel closed when the worker emits a terminal event (completed,
    failed, expired, cancelled). Subscribers receive a sentinel and the
    SSE stream returns. Late subscribers re-fetch from DB.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)


RING_SIZE = 1000
TERMINAL_KINDS = {"completed", "failed", "expired", "cancelled"}


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    payload: dict
    ts: float


class _RunChannel:
    """Per-run pub/sub with a bounded replay buffer.

    Not thread-safe — assumes single-threaded asyncio access (FastAPI's
    default).
    """
    __slots__ = ("run_id", "_ring", "_subscribers", "_closed", "_lock")

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        # deque(maxlen) gives O(1) append + automatic eviction
        self._ring: deque[Event] = deque(maxlen=RING_SIZE)
        self._subscribers: set[asyncio.Queue[Optional[Event]]] = set()
        self._closed = False
        self._lock = asyncio.Lock()

    def publish(self, event: Event) -> None:
        """Append to ring + fan out to live subscribers. Lossy on a slow
        subscriber (queue full) — that subscriber falls back to ring
        replay on reconnect via Last-Event-ID."""
        if self._closed:
            return
        self._ring.append(event)
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)
        if event.kind in TERMINAL_KINDS:
            self._closed = True
            # Wake every subscriber with a sentinel; SSE generator returns.
            for q in list(self._subscribers):
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    def replay_since(self, last_seq: int) -> list[Event]:
        """Return ring events with ``seq > last_seq``. O(n) but n ≤ 1000."""
        return [e for e in self._ring if e.seq > last_seq]

    async def subscribe(self) -> asyncio.Queue:
        """Add a subscriber and return its queue. Caller must call
        ``unsubscribe`` on disconnect."""
        q: asyncio.Queue = asyncio.Queue(maxsize=2 * RING_SIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def closed(self) -> bool:
        return self._closed


# ── Module-level registry ───────────────────────────────────────────────────


_CHANNELS: dict[str, _RunChannel] = {}


def get_or_create(run_id: str) -> _RunChannel:
    """Return the channel for ``run_id``, creating one if it doesn't exist.

    A closed channel is RETURNED, not replaced — its ring buffer is the
    canonical replay window for late SSE consumers. ``drop()`` is the
    only path that removes a channel from the registry.
    """
    ch = _CHANNELS.get(run_id)
    if ch is None:
        ch = _RunChannel(run_id)
        _CHANNELS[run_id] = ch
    return ch


def get(run_id: str) -> Optional[_RunChannel]:
    """Return the existing channel without creating one."""
    return _CHANNELS.get(run_id)


def publish(run_id: str, *, seq: int, kind: str, payload: dict, ts: float) -> None:
    """Public publish API used by the worker. Always safe — creates the
    channel lazily even if no subscribers exist yet."""
    get_or_create(run_id).publish(Event(seq=seq, kind=kind, payload=payload, ts=ts))


def drop(run_id: str) -> None:
    """Remove a closed channel from the registry. Called by maintenance
    to bound memory; safe to omit (channels with no publishers and no
    subscribers GC eventually)."""
    ch = _CHANNELS.get(run_id)
    if ch is not None and ch.closed and not ch._subscribers:
        _CHANNELS.pop(run_id, None)


# ── SSE consumer helper ─────────────────────────────────────────────────────


async def stream_events(
    run_id: str,
    *,
    last_event_id: int = 0,
    keepalive_sec: float = 15.0,
) -> AsyncIterator[Event]:
    """Yield events for an SSE consumer.

    First yields any ring entries with ``seq > last_event_id`` (replay).
    Then subscribes and yields live events until the channel closes
    (terminal event seen) OR the consumer cancels the iterator.

    Keepalive: when idle for ``keepalive_sec``, yield a sentinel ``Event``
    with kind=``__keepalive__`` so the SSE handler can emit a
    ``: keepalive\\n\\n`` line. Consumer filters those out of the wire
    output as needed.
    """
    ch = get_or_create(run_id)
    # Replay first
    for ev in ch.replay_since(last_event_id):
        yield ev
        last_event_id = ev.seq
    if ch.closed:
        return

    q = await ch.subscribe()
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=keepalive_sec)
            except asyncio.TimeoutError:
                yield Event(seq=last_event_id, kind="__keepalive__",
                            payload={}, ts=0.0)
                continue
            if ev is None:
                # Terminal sentinel
                return
            yield ev
            last_event_id = ev.seq
    finally:
        ch.unsubscribe(q)
