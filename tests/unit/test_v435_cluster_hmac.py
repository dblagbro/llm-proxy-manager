"""v4.3.5 — cluster HMAC verification for the coordinator-hub team's
read-only admin endpoint (``/api/admin/external-usage-summary``).

Closes the coordinator-hub team's 2026-05-20 request for an
HMAC-authenticated path so they can surface ``external_usage_snapshot``
data on the hub UI without holding an admin session cookie.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.auth.cluster_hmac import require_cluster_hmac


_SECRET = "test-shared-secret-for-cluster-hmac"


def _make_request(path: str, body: bytes = b"") -> Request:
    """Minimal starlette Request stub with a body() coroutine."""

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
    }

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, _receive)


def _sign(ts: int, path: str, body: bytes, secret: str = _SECRET) -> str:
    signed = f"{ts}{path}".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


# ── happy path ─────────────────────────────────────────────────────


def test_hmac_accepts_valid_signature(monkeypatch):
    monkeypatch.setenv("COORDINATOR_HMAC_KEY", _SECRET)
    ts = int(time.time())
    path = "/api/admin/external-usage-summary"
    sig = _sign(ts, path, b"")
    req = _make_request(path)
    ok = asyncio.run(require_cluster_hmac(req, str(ts), sig))
    assert ok is True


def test_hmac_accepts_uppercase_signature(monkeypatch):
    """Callers may send the hex in mixed case; verification is case-
    insensitive thanks to the .lower() normalization in compare."""
    monkeypatch.setenv("COORDINATOR_HMAC_KEY", _SECRET)
    ts = int(time.time())
    path = "/api/admin/external-usage-summary"
    sig = _sign(ts, path, b"").upper()
    req = _make_request(path)
    ok = asyncio.run(require_cluster_hmac(req, str(ts), sig))
    assert ok is True


# ── failure modes ─────────────────────────────────────────────────


def test_hmac_rejects_when_secret_unset(monkeypatch):
    monkeypatch.delenv("COORDINATOR_HMAC_KEY", raising=False)
    ts = int(time.time())
    path = "/api/admin/external-usage-summary"
    sig = _sign(ts, path, b"")
    req = _make_request(path)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(require_cluster_hmac(req, str(ts), sig))
    assert ex.value.status_code == 503
    assert "COORDINATOR_HMAC_KEY" in ex.value.detail


def test_hmac_rejects_missing_headers(monkeypatch):
    monkeypatch.setenv("COORDINATOR_HMAC_KEY", _SECRET)
    req = _make_request("/api/admin/external-usage-summary")
    with pytest.raises(HTTPException) as ex:
        asyncio.run(require_cluster_hmac(req, None, None))
    assert ex.value.status_code == 401


def test_hmac_rejects_non_numeric_timestamp(monkeypatch):
    monkeypatch.setenv("COORDINATOR_HMAC_KEY", _SECRET)
    req = _make_request("/api/admin/external-usage-summary")
    with pytest.raises(HTTPException) as ex:
        asyncio.run(require_cluster_hmac(req, "not-a-number", "deadbeef"))
    assert ex.value.status_code == 401
    assert "epoch" in ex.value.detail.lower()


def test_hmac_rejects_stale_timestamp(monkeypatch):
    monkeypatch.setenv("COORDINATOR_HMAC_KEY", _SECRET)
    ts = int(time.time()) - 120  # 2 minutes ago
    path = "/api/admin/external-usage-summary"
    sig = _sign(ts, path, b"")
    req = _make_request(path)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(require_cluster_hmac(req, str(ts), sig))
    assert ex.value.status_code == 401
    assert "window" in ex.value.detail.lower()


def test_hmac_rejects_future_timestamp(monkeypatch):
    monkeypatch.setenv("COORDINATOR_HMAC_KEY", _SECRET)
    ts = int(time.time()) + 120
    path = "/api/admin/external-usage-summary"
    sig = _sign(ts, path, b"")
    req = _make_request(path)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(require_cluster_hmac(req, str(ts), sig))
    assert ex.value.status_code == 401


def test_hmac_rejects_wrong_signature(monkeypatch):
    monkeypatch.setenv("COORDINATOR_HMAC_KEY", _SECRET)
    ts = int(time.time())
    path = "/api/admin/external-usage-summary"
    req = _make_request(path)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(require_cluster_hmac(req, str(ts), "0" * 64))
    assert ex.value.status_code == 401
    assert "signature" in ex.value.detail.lower()


def test_hmac_rejects_signature_for_different_path(monkeypatch):
    """A signature valid for path A must not authorize a request to path B."""
    monkeypatch.setenv("COORDINATOR_HMAC_KEY", _SECRET)
    ts = int(time.time())
    sig_for_a = _sign(ts, "/api/admin/external-usage-summary", b"")
    req_for_b = _make_request("/api/admin/something-else")
    with pytest.raises(HTTPException) as ex:
        asyncio.run(require_cluster_hmac(req_for_b, str(ts), sig_for_a))
    assert ex.value.status_code == 401


def test_hmac_rejects_signature_with_wrong_secret(monkeypatch):
    """The server's secret must match the client's signing secret."""
    monkeypatch.setenv("COORDINATOR_HMAC_KEY", "server-secret-X")
    ts = int(time.time())
    path = "/api/admin/external-usage-summary"
    sig = _sign(ts, path, b"", secret="client-secret-Y")
    req = _make_request(path)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(require_cluster_hmac(req, str(ts), sig))
    assert ex.value.status_code == 401


# ── boundary: ±60s window inclusive ───────────────────────────────


def test_hmac_accepts_timestamp_at_boundary(monkeypatch):
    """At exactly -60s the timestamp is still within window."""
    monkeypatch.setenv("COORDINATOR_HMAC_KEY", _SECRET)
    ts = int(time.time()) - 60
    path = "/api/admin/external-usage-summary"
    sig = _sign(ts, path, b"")
    req = _make_request(path)
    ok = asyncio.run(require_cluster_hmac(req, str(ts), sig))
    assert ok is True
