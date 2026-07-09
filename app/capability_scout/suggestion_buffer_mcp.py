"""v5.12.2 — Ship 1.1 of v5.10: MCP-native dual-emit half.

Pattern lifted from upstream-review of ccproxy's NotificationBuffer
(2026-06-29 audit). Path A callers (those with an active /mcp
session) can't receive a notification directly from the /v1/messages
response path because the two transports are independent. The fix:
buffer notifications per-api_key in process-local memory, and drain
the buffer on the NEXT MCP tool call from that key — delivered via
FastMCP's ``ctx.info()`` so it lands in the caller's MCP transport
as a standard ``notifications/message`` event.

Trade-offs vs an immediate-push model:
- + No need to track active MCP sessions across our worker
  topology (which would require shared state we don't have).
- + Notifications survive a brief MCP disconnect/reconnect because
  the buffer persists in process memory.
- - Latency: caller doesn't see the suggestion until their next MCP
  tool call. For Path A users, this is typically within seconds of
  the LLM response that triggered the suggestion (they pick up the
  tool call hint and then immediately call list_tools or similar).
- - Process-local: a roll/restart drops buffered notifications.
  Acceptable for observability; not for compliance enforcement.

Overflow + TTL semantics borrowed from ccproxy's buffer:
- Per-key buffer cap = 32 notifications. Beyond that, oldest is
  dropped with a synthetic ``ccproxy_buffer_overflow``-style marker
  prepended to the next drain.
- TTL = 1 hour. Entries older than that are pruned on each touch.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


_MAX_PER_KEY = 32
_TTL_SEC = 3600.0


@dataclass
class _Notification:
    """One queued MCP notifications/message payload."""
    ts: float
    body: dict[str, Any]


class _NotificationBuffer:
    """Thread-safe per-api_key buffer of pending MCP notifications.

    Lock granularity is one mutex for the whole buffer. The notification
    rates we expect (≤1/s/api_key) make per-key locks unnecessary.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, list[_Notification]] = {}
        self._overflow_seen: dict[str, int] = {}

    def push(self, api_key_id: str, body: dict[str, Any]) -> None:
        """Append a notification for ``api_key_id``. Drops oldest on
        overflow and records the overflow count for the next drain."""
        if not api_key_id or not isinstance(body, dict):
            return
        now = time.time()
        with self._lock:
            bucket = self._buckets.setdefault(api_key_id, [])
            # Prune expired entries on every touch so the buffer
            # doesn't carry stale notifications past TTL.
            cutoff = now - _TTL_SEC
            if bucket and bucket[0].ts < cutoff:
                self._buckets[api_key_id] = [n for n in bucket if n.ts >= cutoff]
                bucket = self._buckets[api_key_id]
            bucket.append(_Notification(ts=now, body=body))
            if len(bucket) > _MAX_PER_KEY:
                drop = len(bucket) - _MAX_PER_KEY
                self._buckets[api_key_id] = bucket[drop:]
                self._overflow_seen[api_key_id] = (
                    self._overflow_seen.get(api_key_id, 0) + drop
                )

    def drain(self, api_key_id: str) -> list[dict[str, Any]]:
        """Return + clear pending notifications for ``api_key_id``.

        Returns the list of body payloads in arrival order. If
        overflow events were recorded since the last drain, prepends
        a synthetic ``ccproxy_buffer_overflow``-style marker (we keep
        ccproxy's exact event type for cross-tool consumer
        compatibility — bots that parse buffer-overflow markers from
        ccproxy will accept ours).
        """
        if not api_key_id:
            return []
        with self._lock:
            bucket = self._buckets.pop(api_key_id, [])
            overflow = self._overflow_seen.pop(api_key_id, 0)
        now = time.time()
        cutoff = now - _TTL_SEC
        # Drop expired-at-drain-time entries (covers the case where the
        # next push raced TTL pruning).
        fresh = [n.body for n in bucket if n.ts >= cutoff]
        if overflow > 0:
            fresh.insert(0, {
                "type": "ccproxy_buffer_overflow",
                "dropped_events": overflow,
                "reason": "per_key_buffer_cap",
            })
        return fresh

    def peek_size(self, api_key_id: Optional[str] = None) -> int:
        """Read-only buffer-depth count. ``None`` returns global size."""
        with self._lock:
            if api_key_id is None:
                return sum(len(b) for b in self._buckets.values())
            return len(self._buckets.get(api_key_id, []))


# Module-level singleton — the buffer is process-local by design.
BUFFER = _NotificationBuffer()


def push_suggestion_notification(
    api_key_id: Optional[str],
    tool: str,
    score: int,
    why: str,
) -> None:
    """Convenience: build the MCP notifications/message body for a
    capability suggestion and push to the per-key buffer.

    The body shape mirrors what FastMCP's ``ctx.info()`` produces —
    consumers that parse ccproxy's notification stream will accept
    it without changes.
    """
    if not api_key_id or not tool:
        return
    try:
        BUFFER.push(api_key_id, {
            "type": "proxy_mcp_suggestion",
            "tool": tool,
            "score": score,
            "why": why,
            "ts": time.time(),
        })
    except Exception as exc:
        logger.debug("push_suggestion_notification failed: %s", exc)


def drain_pending(api_key_id: Optional[str]) -> list[dict[str, Any]]:
    """Wrapper that the MCP server's tool-call hook calls at the start
    of each tool execution to fetch pending notifications for the
    caller. Returns ``[]`` when there's nothing to deliver."""
    if not api_key_id:
        return []
    try:
        return BUFFER.drain(api_key_id)
    except Exception as exc:
        logger.debug("drain_pending failed: %s", exc)
        return []
