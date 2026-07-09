"""v5.14.2 — cluster-sync peer-attempt metric (closes #492 escalation trigger).

Lightweight in-process ring buffer of recent peer-sync attempts (POST to
``/cluster/sync``). The originator already inspects the peer response at
``app/cluster/manager.py:783``; pre-v5.14.2 the only signal was a
``logger.warning`` to stdout, so a sustained 403 stream from a misconfigured
peer was invisible to any monitoring downstream of stdout (i.e. all of it).

This module gives us:
- ``record_attempt(peer_id, status)`` — called by ``push_sync`` after each POST
- ``snapshot()`` — rolling 1h summary surfaced via ``/health.clusterSync``
- ``recent_403_pct`` field that drives the v5.14.2 alert worker

Design choices:
- Per-process. Multi-worker (gunicorn -w N) aggregation is intentionally
  skipped — peer-sync attempts originate from ONE worker per node (the
  scheduler that drives ``push_sync``), so per-process is per-node-effective.
  If we ever move sync to a fan-out across workers we revisit.
- No DB. Adding a row per peer-sync attempt would write ~1k rows/hour per
  peer per node — disproportionate to the diagnostic value. Ring buffer
  costs zero disk.
- Capped at ``_MAX_ATTEMPTS`` entries. Anything older than ``_WINDOW_SEC``
  is excluded from snapshot but stays in memory until the buffer fills.

The companion alert worker
(``app/monitoring/cluster_sync_403_monitor.py``) reads ``snapshot()`` every
``cluster_sync_403_monitor_interval_sec`` and emits an activity_log warning
when ``recent_403_pct`` crosses ``cluster_sync_403_alert_threshold_pct``.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Optional, Tuple

# (timestamp, peer_id, status_code)
_AttemptRow = Tuple[float, str, int]

_MAX_ATTEMPTS = 2048
_WINDOW_SEC = 3600  # 1h rolling window

_lock = threading.Lock()
_attempts: Deque[_AttemptRow] = deque(maxlen=_MAX_ATTEMPTS)
_last_success_ts: Optional[float] = None
_last_attempt_ts: Optional[float] = None
_last_attempt_status: Optional[int] = None


def record_attempt(peer_id: str, status_code: int) -> None:
    """Record a peer-sync POST result. ``status_code = 0`` for transport errors
    (connection refused, timeout) — distinguished from server-replied non-200
    so the 403-rate stat isn't conflated with network outages."""
    global _last_success_ts, _last_attempt_ts, _last_attempt_status
    now = time.time()
    with _lock:
        _attempts.append((now, peer_id, int(status_code)))
        _last_attempt_ts = now
        _last_attempt_status = int(status_code)
        if status_code == 200:
            _last_success_ts = now


def reset_for_tests() -> None:
    global _last_success_ts, _last_attempt_ts, _last_attempt_status
    with _lock:
        _attempts.clear()
        _last_success_ts = None
        _last_attempt_ts = None
        _last_attempt_status = None


def snapshot() -> dict:
    """Surfaced via ``/health.clusterSync``. Always returns; never raises."""
    now = time.time()
    cutoff = now - _WINDOW_SEC
    with _lock:
        recent = [a for a in _attempts if a[0] >= cutoff]
        last_success_ts = _last_success_ts
        last_attempt_ts = _last_attempt_ts
        last_attempt_status = _last_attempt_status

    total = len(recent)
    status_403 = sum(1 for _, _, s in recent if s == 403)
    status_200 = sum(1 for _, _, s in recent if s == 200)
    status_transport = sum(1 for _, _, s in recent if s == 0)
    status_other = total - status_403 - status_200 - status_transport

    pct_403 = (status_403 / total * 100.0) if total > 0 else 0.0

    # cluster_sync_fresh: a successful sync occurred within the last 600s.
    # 600s chosen to be 2x the typical sync interval (~5min) so a single
    # missed sync doesn't flip fresh→stale.
    cluster_sync_fresh = (
        last_success_ts is not None
        and (now - last_success_ts) <= 600.0
    )

    return {
        "attempts_1h": total,
        "status_200_1h": status_200,
        "status_403_1h": status_403,
        "status_other_1h": status_other,
        "status_transport_err_1h": status_transport,
        "recent_403_pct": round(pct_403, 2),
        "last_attempt_at": (
            int(last_attempt_ts) if last_attempt_ts is not None else None
        ),
        "last_attempt_status": last_attempt_status,
        "last_success_at": (
            int(last_success_ts) if last_success_ts is not None else None
        ),
        "cluster_sync_fresh": cluster_sync_fresh,
    }
