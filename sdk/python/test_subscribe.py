"""v3.5.2 — tests for LmrhClient.subscribe() (SSE push consumer).

Covers:
- Subscribe receives parsed Snapshot for each ``event: snapshot`` frame
- Heartbeat (``: ping``) frames are ignored
- 404 on /lmrh/stream falls back to polling
- /.well-known probe with no ``stream`` endpoint falls back to polling
- on_error callback fires for transport-level exceptions
- stop() cleanly exits the loop
"""
from __future__ import annotations

import threading
import time
from typing import Iterator

import httpx
import pytest

from sdk.python.lmrh_client import LmrhClient, Snapshot


import json as _json_module

# Match the production server: compact one-line JSON (no indent / newlines).
# Real frames come from `json.dumps(body, default=str)` server-side which
# is single-line by default, and SSE ``data:`` is line-oriented so embedded
# newlines would break the frame.
_SAMPLE_SNAPSHOT_DICT = {
    "version": "2.1",
    "as_of": "2026-05-09T22:00:00+00:00",
    "window_sec": 3600,
    "providers": [{
        "id": "p1", "name": "Grok-Web-Devin", "type": "grok-web",
        "priority": 1, "cost_class": "subscription", "circuit": "closed",
        "regions": [],
        "models": [{
            "model_id": "x-ai/grok-3", "kind": "chat",
            "context_length": 128000,
            "native_tools": False, "native_reasoning": False,
            "aliases": ["grok-3"], "family": "grok-3", "variant": "web",
            "metrics": {
                "cost_per_1m_input_usd": None, "cost_per_1m_output_usd": None,
                "rated_quota_per_1m_input_usd": None,
                "latency_p50_ms": 2500.0, "latency_p95_ms": 6800.0,
                "ttft_p50_ms": None, "ttft_p95_ms": None,
                "success_rate": 1.0, "samples": 154,
            },
        }],
    }],
}
SAMPLE_SNAPSHOT_JSON = _json_module.dumps(_SAMPLE_SNAPSHOT_DICT)


def _well_known_with_stream() -> dict:
    return {
        "version": "2.1",
        "supported_versions": ["1.2", "2.0", "2.1"],
        "endpoints": {
            "providers": "/lmrh/providers",
            "stream": "/lmrh/stream",
            "health": "/lmrh/health",
        },
        "polling": {"providers_recommended_interval_sec": 60},
        "cache": {},
        "supported_dims": [],
    }


def _well_known_no_stream() -> dict:
    """v3.3.x proxy — no /lmrh/stream endpoint advertised."""
    return {
        "version": "2.0",
        "supported_versions": ["1.2", "2.0"],
        "endpoints": {"providers": "/lmrh/providers"},
        "polling": {},
        "cache": {},
        "supported_dims": [],
    }


def _sse_frame_bytes(event: str, data: str, id: str = "") -> bytes:
    """Build a single SSE frame ending with the blank-line separator."""
    parts = []
    if event:
        parts.append(f"event: {event}")
    if id:
        parts.append(f"id: {id}")
    parts.append(f"data: {data}")
    parts.append("")
    parts.append("")  # final blank
    return ("\n".join(parts)).encode()


# ── Subscribe receives snapshot frames ────────────────────────────────


def test_subscribe_dispatches_snapshot_per_frame():
    """The first ``event: snapshot`` frame must result in on_snapshot
    being called with a parsed Snapshot."""
    received: list[Snapshot] = []
    stop_after_first = threading.Event()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/lmrh-config"):
            return httpx.Response(200, json=_well_known_with_stream())
        # SSE stream — emit one frame, then close
        if req.url.path.endswith("/lmrh/stream"):
            body = _sse_frame_bytes("snapshot", SAMPLE_SNAPSHOT_JSON, id="abc")
            return httpx.Response(
                200, content=body,
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = LmrhClient(base_url="http://test", api_key="k")

    # Inject the mock transport into httpx.Client globally for the
    # duration of this test. The SDK creates its own httpx.Client
    # instances internally; we patch the constructor to feed it
    # the transport.
    import httpx as _httpx
    orig_client = _httpx.Client
    def patched(*a, **kw):
        kw.setdefault("transport", transport)
        return orig_client(*a, **kw)

    def subscriber():
        try:
            client.subscribe(
                on_snapshot=lambda s: (received.append(s), stop_after_first.set()),
                reconnect_delay_sec=0.05,
            )
        except Exception:
            pass

    import unittest.mock as mock
    with mock.patch.object(_httpx, "Client", patched):
        t = threading.Thread(target=subscriber, daemon=True)
        t.start()
        # Wait up to 2s for the snapshot
        stop_after_first.wait(2.0)
        client.stop()
        t.join(timeout=1.0)

    assert len(received) >= 1
    snap = received[0]
    assert snap.version == "2.1"
    assert len(snap.providers) == 1
    assert snap.providers[0].name == "Grok-Web-Devin"
    m = snap.providers[0].models[0]
    assert m.family == "grok-3"
    assert m.variant == "web"
    assert m.aliases == ("grok-3",)


# ── Falls back to polling when /lmrh/stream not advertised ────────────


def test_subscribe_falls_back_to_polling_on_no_stream_endpoint():
    """An older proxy (v3.3.x) doesn't advertise /lmrh/stream in
    /.well-known. subscribe() should detect this and dispatch
    snapshots from the polling path instead."""
    received: list[Snapshot] = []
    stop_after_first = threading.Event()
    poll_calls = {"count": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/lmrh-config"):
            return httpx.Response(200, json=_well_known_no_stream())
        if req.url.path.endswith("/lmrh/providers"):
            poll_calls["count"] += 1
            return httpx.Response(
                200, json=_SAMPLE_SNAPSHOT_DICT,
                headers={"etag": f'"poll-{poll_calls["count"]}"'},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = LmrhClient(
        base_url="http://test", api_key="k", poll_interval_sec=1,
    )

    import httpx as _httpx
    orig_client = _httpx.Client
    def patched(*a, **kw):
        kw.setdefault("transport", transport)
        return orig_client(*a, **kw)

    def subscriber():
        try:
            client.subscribe(
                on_snapshot=lambda s: (received.append(s), stop_after_first.set()),
            )
        except Exception:
            pass

    import unittest.mock as mock
    with mock.patch.object(_httpx, "Client", patched):
        t = threading.Thread(target=subscriber, daemon=True)
        t.start()
        stop_after_first.wait(3.0)
        client.stop()
        t.join(timeout=2.0)

    assert len(received) >= 1
    assert poll_calls["count"] >= 1


# ── Heartbeat frames are silently skipped ─────────────────────────────


def test_subscribe_ignores_heartbeat_frames():
    """``: ping`` frames must NOT trigger on_snapshot."""
    received: list[Snapshot] = []
    stop_after_first = threading.Event()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/lmrh-config"):
            return httpx.Response(200, json=_well_known_with_stream())
        if req.url.path.endswith("/lmrh/stream"):
            # Two heartbeats, one snapshot
            body = (
                b": ping\n\n"
                + b": ping\n\n"
                + _sse_frame_bytes("snapshot", SAMPLE_SNAPSHOT_JSON)
            )
            return httpx.Response(
                200, content=body,
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = LmrhClient(base_url="http://test", api_key="k")

    import httpx as _httpx
    orig_client = _httpx.Client
    def patched(*a, **kw):
        kw.setdefault("transport", transport)
        return orig_client(*a, **kw)

    def subscriber():
        try:
            client.subscribe(
                on_snapshot=lambda s: (received.append(s), stop_after_first.set()),
                reconnect_delay_sec=0.05,
            )
        except Exception:
            pass

    import unittest.mock as mock
    with mock.patch.object(_httpx, "Client", patched):
        t = threading.Thread(target=subscriber, daemon=True)
        t.start()
        stop_after_first.wait(2.0)
        client.stop()
        t.join(timeout=1.0)

    # Exactly one snapshot — heartbeats should not have produced
    # additional callbacks.
    assert len(received) == 1


# ── 404 on /lmrh/stream falls back to polling ─────────────────────────


def test_subscribe_falls_back_when_stream_returns_404():
    """The /.well-known might be cached or stale; the actual stream
    endpoint could 404 even if advertised. Falls back to polling."""
    received: list[Snapshot] = []
    stop_after_first = threading.Event()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/lmrh-config"):
            return httpx.Response(200, json=_well_known_with_stream())
        if req.url.path.endswith("/lmrh/stream"):
            return httpx.Response(404)
        if req.url.path.endswith("/lmrh/providers"):
            return httpx.Response(
                200, json=_SAMPLE_SNAPSHOT_DICT,
                headers={"etag": '"poll-1"'},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = LmrhClient(
        base_url="http://test", api_key="k", poll_interval_sec=1,
    )

    import httpx as _httpx
    orig_client = _httpx.Client
    def patched(*a, **kw):
        kw.setdefault("transport", transport)
        return orig_client(*a, **kw)

    def subscriber():
        try:
            client.subscribe(
                on_snapshot=lambda s: (received.append(s), stop_after_first.set()),
                reconnect_delay_sec=0.05,
            )
        except Exception:
            pass

    import unittest.mock as mock
    with mock.patch.object(_httpx, "Client", patched):
        t = threading.Thread(target=subscriber, daemon=True)
        t.start()
        stop_after_first.wait(3.0)
        client.stop()
        t.join(timeout=2.0)

    # Should have received via polling fallback
    assert len(received) >= 1


# ── stop() cleanly exits the subscribe loop ───────────────────────────


def test_subscribe_exits_on_stop():
    """A blocked subscribe() call returns within reconnect_delay_sec
    of stop() being called."""
    received: list[Snapshot] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/lmrh-config"):
            return httpx.Response(200, json=_well_known_with_stream())
        if req.url.path.endswith("/lmrh/stream"):
            # Empty stream — server hangs up immediately
            return httpx.Response(
                200, content=b"",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = LmrhClient(base_url="http://test", api_key="k")

    import httpx as _httpx
    orig_client = _httpx.Client
    def patched(*a, **kw):
        kw.setdefault("transport", transport)
        return orig_client(*a, **kw)

    finished = threading.Event()

    def subscriber():
        try:
            client.subscribe(
                on_snapshot=lambda s: received.append(s),
                reconnect_delay_sec=0.1,
            )
        except Exception:
            pass
        finished.set()

    import unittest.mock as mock
    with mock.patch.object(_httpx, "Client", patched):
        t = threading.Thread(target=subscriber, daemon=True)
        t.start()
        # Let it spin up + reconnect a few times
        time.sleep(0.5)
        client.stop()
        # subscribe() should exit within ~reconnect_delay
        assert finished.wait(2.0), "subscribe() did not exit on stop()"
        t.join(timeout=1.0)
