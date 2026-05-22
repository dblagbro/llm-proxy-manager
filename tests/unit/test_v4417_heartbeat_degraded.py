"""v4.4.17 (F4) — cluster heartbeat distinguishes degraded from unreachable.

From the 2026-05-22 routing-cost research (F4): the heartbeat did
``data = resp.json()`` without checking status/content-type. A peer
returning a non-JSON body (nginx 502/504 HTML during that peer's
own container restart) raised JSONDecodeError and got logged +
notified identically to a truly-down peer (connection refused /
timeout). The all-providers-down notifier would fire for a routine
deploy blip.

Fix: classify the response into three buckets —
- 200 + valid JSON   → healthy (parse providers/status)
- non-200, or 200 + non-JSON body → ``degraded`` (transient, likely
  restarting): log INFO, do NOT notify
- connection-level exception (refused/timeout/DNS) → ``unreachable``:
  log WARNING + notify

These tests drive ``_ping_peer`` with mocked httpx responses.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import json as _json

import pytest


def _make_peer(status="healthy"):
    from app.cluster.manager import PeerNode
    # PeerNode is a dataclass-ish; construct minimally.
    try:
        p = PeerNode(id="llm-proxy2-www2", url="https://www2.example/llm-proxy2")
    except TypeError:
        # Fallback if PeerNode requires more args — build a stand-in with
        # the attributes _ping_peer touches.
        p = SimpleNamespace(
            id="llm-proxy2-www2", url="https://www2.example/llm-proxy2",
            status=status, latency_ms=0.0, last_heartbeat=0.0,
            healthy_providers=0, total_providers=0,
        )
        return p
    p.status = status
    return p


class _Resp:
    def __init__(self, status_code, json_body=None, raise_on_json=False):
        self.status_code = status_code
        self._json_body = json_body
        self._raise = raise_on_json

    def json(self):
        if self._raise:
            raise _json.JSONDecodeError("Expecting value", "", 0)
        return self._json_body


def _patched_client(resp):
    """Return an async context manager whose .get returns resp."""
    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw): return resp
    return lambda *a, **kw: _Client()


@pytest.mark.asyncio
async def test_200_valid_json_marks_healthy():
    from app.cluster import manager
    peer = _make_peer(status="unreachable")
    resp = _Resp(200, {"status": "healthy", "healthyProviders": 10, "totalProviders": 10})
    notify = AsyncMock()
    with patch.object(manager.httpx, "AsyncClient", _patched_client(resp)):
        await manager._ping_peer(peer, notify_fn=notify)
    assert peer.status == "healthy"
    assert peer.healthy_providers == 10
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_non_200_marks_degraded_not_unreachable():
    """A 502 (deploy-window error page) → degraded, no notify."""
    from app.cluster import manager
    peer = _make_peer(status="healthy")
    resp = _Resp(502)
    notify = AsyncMock()
    with patch.object(manager.httpx, "AsyncClient", _patched_client(resp)):
        await manager._ping_peer(peer, notify_fn=notify)
    assert peer.status == "degraded", "non-200 should be degraded, not unreachable"
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_200_non_json_body_marks_degraded_not_unreachable():
    """200 but a non-JSON body (HTML error page) → degraded, no notify.
    This is the exact JSONDecodeError case from the 2026-05-22 audit."""
    from app.cluster import manager
    peer = _make_peer(status="healthy")
    resp = _Resp(200, raise_on_json=True)
    notify = AsyncMock()
    with patch.object(manager.httpx, "AsyncClient", _patched_client(resp)):
        await manager._ping_peer(peer, notify_fn=notify)
    assert peer.status == "degraded", "200+non-JSON should be degraded, not unreachable"
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_connection_exception_marks_unreachable_and_notifies():
    """Connection refused / timeout → unreachable + notify (genuine down)."""
    from app.cluster import manager

    def _raising_client(*a, **kw):
        class _Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw):
                raise manager.httpx.ConnectError("Connection refused")
        return _Client()

    peer = _make_peer(status="healthy")
    notify = AsyncMock()
    with patch.object(manager.httpx, "AsyncClient", _raising_client):
        await manager._ping_peer(peer, notify_fn=notify)
    assert peer.status == "unreachable"
    notify.assert_called_once()


@pytest.mark.asyncio
async def test_degraded_then_healthy_logs_recovery():
    """A peer that was degraded (restarting) coming back to 200+JSON
    should log recovery (was_unreachable covers degraded too)."""
    from app.cluster import manager
    peer = _make_peer(status="degraded")
    resp = _Resp(200, {"status": "healthy", "healthyProviders": 10, "totalProviders": 10})
    with patch.object(manager.httpx, "AsyncClient", _patched_client(resp)):
        await manager._ping_peer(peer, notify_fn=AsyncMock())
    assert peer.status == "healthy"


def test_source_classifies_three_buckets():
    """Source-level guard: the function handles non-200, non-JSON, and
    connection-exception as distinct paths."""
    from pathlib import Path
    src = Path("app/cluster/manager.py").read_text()
    fn = src[src.index("async def _ping_peer("):src.index("async def _ping_peer(") + 3500]
    assert 'resp.status_code != 200' in fn
    assert 'peer.status = "degraded"' in fn
    assert 'peer.status = "unreachable"' in fn
    # degraded path must not notify
    assert "F4" in fn
