"""v5.22.68: grok bridge sign-in resets breaker everywhere + clears keepalive gate;
signed-out cause is surfaced in the attention row."""

import asyncio
from types import SimpleNamespace

import app.cluster.breaker_reset as br
import app.monitoring.keepalive as ka
import app.providers.grok_web_bridge as gwb
import app.routing.attention as att
import app.routing.circuit_breaker as cb


def _grok(pid="g1", url="http://bridge:8443"):
    return SimpleNamespace(
        id=pid, enabled=True, provider_type="grok-web", extra_config={"bridge_url": url}
    )


def test_clear_chronic_gate():
    ka._chronic_backoff_until["chronic_cb_g1"] = 9e12
    ka._probe_backoff_until["g1"] = 9e12
    assert ka.clear_chronic_gate("g1") is True
    assert "chronic_cb_g1" not in ka._chronic_backoff_until
    assert "g1" not in ka._probe_backoff_until
    assert ka.clear_chronic_gate("g1") is False


def test_reset_local_clears_gate(monkeypatch):
    async def fake_close(pid):
        pass

    monkeypatch.setattr(cb, "force_close", fake_close)
    monkeypatch.setattr(cb, "clear_auth_failure", lambda pid: None)
    ka._chronic_backoff_until["chronic_cb_g2"] = 9e12
    asyncio.run(br.reset_local("g2"))
    assert "chronic_cb_g2" not in ka._chronic_backoff_until


def _patch(monkeypatch, status):
    calls = []

    async def fake_status(ec, cookie="", timeout=5.0):
        calls.append(ec["bridge_url"])
        return status

    async def fake_reset(pid):
        calls.append(("reset", pid))
        return []

    monkeypatch.setattr(gwb, "bridge_logged_in", fake_status)
    monkeypatch.setattr(br, "reset_everywhere", fake_reset)
    return calls


def test_signed_in_resets_and_clears(monkeypatch):
    calls = _patch(monkeypatch, True)
    ka._chronic_backoff_until["chronic_cb_g1"] = 9e12
    other = SimpleNamespace(id="o", enabled=True, provider_type="openai", extra_config={})
    res = asyncio.run(att.grok_bridge_signed_in([_grok(), other]))
    assert res["providers"][0]["reset"] is True and res["providers"][0]["gate_cleared"] is True
    assert ("reset", "g1") in calls and len(res["providers"]) == 1
    assert "chronic_cb_g1" not in ka._chronic_backoff_until


def test_still_signed_out_does_not_reset(monkeypatch):
    calls = _patch(monkeypatch, False)
    res = asyncio.run(att.grok_bridge_signed_in([_grok()]))
    assert res["providers"][0]["reset"] is False
    assert not any(isinstance(c, tuple) for c in calls)


def test_bridge_url_deduped(monkeypatch):
    calls = _patch(monkeypatch, True)
    asyncio.run(att.grok_bridge_signed_in([_grok("a"), _grok("b")]))
    assert calls.count("http://bridge:8443") == 1


def _classify(**kw):
    base = dict(
        provider_id="g1",
        provider_type="grok-web",
        extra_config={"bridge_url": "http://b:1"},
        cb={"state": "open"},
        auth_failure=None,
        billing_since=None,
        backoff=None,
    )
    base.update(kw)
    return att.classify_provider(**base)


def test_auth_failure_message():
    row = _classify(auth_failure={"last_error": "session is logged out", "since": 1.0})
    assert row["reason"] == "logged_out"
    assert row["message"] == att.GROK_SIGNED_OUT_MESSAGE


def test_no_auth_failure_stays_unreachable():
    row = _classify(auth_failure=None)
    assert row["reason"] == "unreachable" and "message" not in row


def test_non_grok_unchanged():
    row = _classify(provider_type="openai", auth_failure={"last_error": "bad", "since": 1.0})
    assert row["reason"] == "auth" and "message" not in row


def test_bridge_status_forwards_admin_cookie(monkeypatch):
    """/api/status on the bridge needs an admin session (token alone is 401),
    so the caller's cookie must ride along."""
    from types import SimpleNamespace as NS

    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers):
            seen["url"], seen["headers"] = url, headers
            return NS(status_code=200, json=lambda: {"logged_in": True})

    monkeypatch.setattr(gwb.httpx, "AsyncClient", FakeClient)
    ec = {"bridge_url": "http://bridge:8443/", "bridge_token": "t"}
    assert asyncio.run(gwb.bridge_logged_in(ec, cookie="session=abc")) is True
    assert seen["url"] == "http://bridge:8443/api/status"
    assert seen["headers"]["cookie"] == "session=abc"
