"""In-process idempotency cache (R4).

R1 already handles idempotency correctly via the ``run_idempotency`` DB
table — the contract is "duplicate POST returns the existing run within
24h". R4 layers a small in-memory cache in front so the hot path (hub
re-fires within milliseconds on a daemon network blip) avoids the DB
lookup.

Cache is per-process; on a multi-node cluster, a duplicate POST that
lands on a different node still hits the DB, which is correct — the DB
is the source of truth and is replicated by /cluster/sync (R5).

24h TTL anchored at ``created_at`` of the original Run (matches the
DB-side check). Bounded size to keep memory predictable; LRU eviction.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import Optional

from app.runs.tokens import DEFAULT_CONTEXT_LENGTH  # unused; here to keep import tight  # noqa


TTL_SEC = 24 * 60 * 60
DEFAULT_MAX_SIZE = 10_000


class _IdempotencyCache:
    """Thread-safe LRU with TTL per entry.

    Key: ``(api_key_id, idempotency_key)`` tuple. Value: ``(run_id,
    created_at)``. Threading lock because FastAPI runs handlers on
    asyncio's default executor for sync code paths and we want to be
    safe regardless of how the cache is touched.
    """
    __slots__ = ("_data", "_lock", "_max_size")

    def __init__(self, max_size: int = DEFAULT_MAX_SIZE) -> None:
        self._data: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()
        self._lock = Lock()
        self._max_size = max_size

    def get(self, api_key_id: str, idempotency_key: str) -> Optional[str]:
        """Return the cached run_id if present + within TTL; else None."""
        k = (api_key_id, idempotency_key)
        with self._lock:
            entry = self._data.get(k)
            if entry is None:
                return None
            run_id, created_at = entry
            if (time.time() - created_at) >= TTL_SEC:
                self._data.pop(k, None)
                return None
            # Mark as recently-used (LRU bump)
            self._data.move_to_end(k)
            return run_id

    def put(self, api_key_id: str, idempotency_key: str,
            run_id: str, created_at: float) -> None:
        k = (api_key_id, idempotency_key)
        with self._lock:
            self._data[k] = (run_id, created_at)
            self._data.move_to_end(k)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)  # evict LRU

    def invalidate(self, api_key_id: str, idempotency_key: str) -> None:
        with self._lock:
            self._data.pop((api_key_id, idempotency_key), None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


_GLOBAL_CACHE = _IdempotencyCache()


def get(api_key_id: str, idempotency_key: str) -> Optional[str]:
    return _GLOBAL_CACHE.get(api_key_id, idempotency_key)


def put(api_key_id: str, idempotency_key: str, run_id: str,
        created_at: float) -> None:
    _GLOBAL_CACHE.put(api_key_id, idempotency_key, run_id, created_at)


def invalidate(api_key_id: str, idempotency_key: str) -> None:
    _GLOBAL_CACHE.invalidate(api_key_id, idempotency_key)


def clear() -> None:
    """Test-only — wipe the cache between cases."""
    _GLOBAL_CACHE.clear()


def size() -> int:
    return len(_GLOBAL_CACHE)
