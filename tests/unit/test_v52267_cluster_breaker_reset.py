"""v5.22.67: a breaker reset (passing Test, re-test now, admin reset) reaches every node."""

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.cluster as cluster_api
import app.api.provider_lifecycle as pl
import app.cluster.breaker_reset as br
import app.cluster.manager as manager
import app.routing.circuit_breaker as cb
from app.config import settings


@pytest.fixture
def calls(monkeypatch):
    c = {"close": [], "clear": []}

    async def fake_close(pid):
        c["close"].append(pid)

    monkeypatch.setattr(cb, "force_close", fake_close)
    monkeypatch.setattr(cb, "clear_auth_failure", lambda pid: c["clear"].append(pid))
    return c


@pytest.fixture
def two_peers(monkeypatch):
    peers = {
        "www2": SimpleNamespace(id="www2", url="http://www2:3000/", status="healthy"),
        "dead": SimpleNamespace(id="dead", url="http://dead:3000", status="unreachable"),
    }
    monkeypatch.setattr(manager, "peers", peers)
    monkeypatch.setattr(settings, "cluster_enabled", True)
    monkeypatch.setattr(settings, "cluster_sync_secret", "s" * 40)
    monkeypatch.setattr(settings, "cluster_node_id", "www1")
    return peers


def test_reset_everywhere_pushes_signed_to_live_peers(calls, two_peers, monkeypatch):
    sent = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers):
            sent.append((url, content, headers))
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(br.httpx, "AsyncClient", FakeClient)
    out = asyncio.run(br.reset_everywhere("p1"))
    assert calls == {"close": ["p1"], "clear": ["p1"]}
    assert out == [{"peer_id": "www2", "status": "ok"}]
    assert len(sent) == 1  # unreachable peer skipped
    url, body, headers = sent[0]
    assert url == "http://www2:3000/cluster/circuit-breaker-reset"
    assert json.loads(body) == {"provider_id": "p1"}
    expected = hmac.new(b"s" * 40, body, hashlib.sha256).hexdigest()
    assert headers["X-Cluster-Sig"] == expected


def test_peer_failure_is_swallowed(calls, two_peers, monkeypatch):
    class Boom:
        def __init__(self, **kw):
            raise OSError("down")

    monkeypatch.setattr(br.httpx, "AsyncClient", Boom)
    out = asyncio.run(br.reset_everywhere("p1"))
    assert out == [{"peer_id": "www2", "status": "unreachable"}]
    assert calls["close"] == ["p1"]


def test_no_fanout_without_secret(calls, two_peers, monkeypatch):
    monkeypatch.setattr(settings, "cluster_sync_secret", None)
    assert asyncio.run(br.reset_everywhere("p1")) == []
    assert calls["close"] == ["p1"]


def _req(body: bytes, sig: str):
    async def _body():
        return body

    return SimpleNamespace(body=_body, headers={"X-Cluster-Sig": sig})


def test_peer_endpoint_requires_valid_hmac(calls, two_peers):
    body = json.dumps({"provider_id": "p1"}).encode()
    with pytest.raises(HTTPException) as e:
        asyncio.run(cluster_api.peer_circuit_breaker_reset(_req(body, "bad")))
    assert e.value.status_code == 401
    assert calls["close"] == []


def test_peer_endpoint_resets_locally_only(calls, two_peers, monkeypatch):
    monkeypatch.setattr(br, "fan_out", lambda pid: pytest.fail("peer must not re-fan-out"))
    body = json.dumps({"provider_id": "p1"}).encode()
    sig = hmac.new(b"s" * 40, body, hashlib.sha256).hexdigest()
    assert asyncio.run(cluster_api.peer_circuit_breaker_reset(_req(body, sig))) == {"ok": True}
    assert calls == {"close": ["p1"], "clear": ["p1"]}


def test_peer_endpoint_rejects_bad_id(calls, two_peers):
    body = json.dumps({"provider_id": ""}).encode()
    sig = hmac.new(b"s" * 40, body, hashlib.sha256).hexdigest()
    with pytest.raises(HTTPException) as e:
        asyncio.run(cluster_api.peer_circuit_breaker_reset(_req(body, sig)))
    assert e.value.status_code == 400


def test_provider_test_endpoint_fans_out(calls, monkeypatch):
    seen = []

    async def fake_everywhere(pid):
        seen.append(pid)
        return [{"peer_id": "www2", "status": "ok"}]

    async def fake_get(db, pid):
        return SimpleNamespace(id=pid, name="p", provider_type="openai", default_model="gpt-x")

    async def fake_test(p):
        return {"success": True}

    monkeypatch.setattr(br, "reset_everywhere", fake_everywhere)
    monkeypatch.setattr(pl, "_get_or_404", fake_get)
    monkeypatch.setattr(pl, "test_provider", fake_test)
    out = asyncio.run(pl.test_provider_endpoint("pid", db=None, _=None))
    assert seen == ["pid"]
    assert out["breaker_reset_peers"] == [{"peer_id": "www2", "status": "ok"}]
