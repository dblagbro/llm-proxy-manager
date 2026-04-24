"""
In-process rate-limit state + check primitives.

Extracted from ``app/auth/keys.py`` in the 2026-04-23 refactor so that
`keys.py` can focus on API-key authentication and the rate-limit
machinery lives in one place.

All state is per-process (not shared across cluster nodes). Cluster-
aware semantics are implemented by dividing the configured limit by
`active_node_count()` so each node enforces its own share. This is
intentionally eventually-consistent — a small burst can slip through
if one node is down and another sees its share grow before heartbeat.

Public API preserved via re-exports in ``app.auth.keys``:
    _rpm_windows, _rpd_buckets, _burst_counters
    _check_rate_limit, _check_rpd, _check_burst
    begin_in_flight, end_in_flight
"""
from __future__ import annotations

import collections
import time

from fastapi import HTTPException

# Re-bindable reference so tests can monkeypatch either this module OR
# the cluster.manager module and still have the override take effect.
from app.cluster import manager as _cluster_manager


def active_node_count() -> int:
    """Indirection layer so monkeypatching `active_node_count` on this
    module or on `cluster.manager` both work."""
    return _cluster_manager.active_node_count()


# ── State (module-level; tests reset these between runs) ─────────────────────

# Sliding-window RPM tracker: key_id → deque of request timestamps
_rpm_windows: dict[str, collections.deque] = {}
# Daily request counter: key_id → (day_bucket_ts, count)
_rpd_buckets: dict[str, tuple[int, int]] = {}
# In-flight concurrency tracker: key_id → count
_burst_counters: dict[str, int] = {}


# ── Checks ───────────────────────────────────────────────────────────────────


def _check_rate_limit(key_id: str, limit_rpm: int) -> None:
    """Raise HTTP 429 if this node's share of limit_rpm is exceeded in the last 60 seconds."""
    nodes = max(1, active_node_count())
    per_node_limit = max(1, limit_rpm // nodes)

    now = time.monotonic()
    window = _rpm_windows.setdefault(key_id, collections.deque())
    cutoff = now - 60.0
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= per_node_limit:
        raise HTTPException(
            429,
            f"Rate limit exceeded: {limit_rpm} requests/minute (cluster-wide)",
            headers={"Retry-After": "60"},
        )
    window.append(now)


def _check_rpd(key_id: str, limit_rpd: int) -> None:
    """Raise HTTP 429 if the UTC-day request count exceeds limit_rpd on this
    node. Per-node share mirrors _check_rate_limit."""
    nodes = max(1, active_node_count())
    per_node_limit = max(1, limit_rpd // nodes)

    today = int(time.time() // 86400)
    bucket_ts, count = _rpd_buckets.get(key_id, (today, 0))
    if bucket_ts != today:
        bucket_ts, count = today, 0
    if count >= per_node_limit:
        raise HTTPException(
            429,
            f"Daily rate limit exceeded: {limit_rpd} requests/day (cluster-wide)",
            headers={"Retry-After": "3600"},
        )
    _rpd_buckets[key_id] = (bucket_ts, count + 1)


def _check_burst(key_id: str, limit: int) -> None:
    """Raise HTTP 429 if in-flight concurrency on this node exceeds `limit`."""
    in_flight = _burst_counters.get(key_id, 0)
    if in_flight >= limit:
        raise HTTPException(
            429,
            f"Concurrency limit exceeded: {limit} in-flight requests",
            headers={"Retry-After": "5"},
        )


def begin_in_flight(key_id: str) -> None:
    _burst_counters[key_id] = _burst_counters.get(key_id, 0) + 1


def end_in_flight(key_id: str) -> None:
    cur = _burst_counters.get(key_id, 0)
    if cur > 0:
        _burst_counters[key_id] = cur - 1
