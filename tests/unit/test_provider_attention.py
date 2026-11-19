"""Provider "needs attention" classification + retest-and-reset helper."""

import asyncio
import concurrent.futures
from types import SimpleNamespace

import pytest

from app.routing import attention as att
from app.routing import circuit_breaker as cb


def _run(coro):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@pytest.fixture(autouse=True)
def clean():
    for d in (cb._local_states, cb._auth_failed, cb._billing_failed):
        d.clear()
    yield
    for d in (cb._local_states, cb._auth_failed, cb._billing_failed):
        d.clear()


def _classify(ptype="openai", **kw):
    base = dict(
        provider_id="p1",
        provider_type=ptype,
        extra_config={},
        cb=None,
        auth_failure=None,
        billing_since=None,
        backoff=None,
    )
    base.update(kw)
    return att.classify_provider(**base)


OPEN = {"state": "open", "hold_down_remaining": 120.0}


def test_healthy_returns_none():
    assert _classify(cb={"state": "closed", "hold_down_remaining": 0}) is None


def test_billing_openai_has_vendor_link():
    r = _classify(cb={"state": "open", "hold_down_remaining": 21000}, billing_since=1.0)
    assert r["reason"] == "billing"
    assert r["billing_url"] == "https://platform.openai.com/settings/organization/limits"
    assert r["can_retest"] and not r["can_replace_key"]


def test_billing_unknown_vendor_has_no_link():
    r = _classify("mistral", cb=OPEN, billing_since=1.0)
    assert r["reason"] == "billing" and r["billing_url"] is None


def test_auth_offers_replace_key():
    r = _classify("openrouter", cb=OPEN, auth_failure={"since": 5.0, "last_error": "401"})
    assert r["reason"] == "auth" and r["can_replace_key"] is True


def test_billing_wins_over_auth():
    r = _classify(cb=OPEN, billing_since=1.0, auth_failure={"since": 5.0, "last_error": "x"})
    assert r["reason"] == "billing"


def test_rate_limited_shows_retry_and_no_actions():
    r = _classify(cb=OPEN, backoff={"backoff_remaining_sec": 300.0})
    assert r["reason"] == "rate_limit" and r["retry_in_sec"] == 300.0
    assert not r["can_retest"] and not r["can_replace_key"]


def test_unreachable_default_when_held():
    r = _classify(cb={"state": "half-open", "hold_down_remaining": 0})
    assert r["reason"] == "unreachable" and r["retry_in_sec"] is None


def test_grok_web_logged_out_links_bridge_login():
    r = _classify(
        "grok-web",
        extra_config={"bridge_url": "http://bridge:8080/"},
        cb=OPEN,
        auth_failure={"since": 1.0, "last_error": "logged out"},
    )
    assert r["reason"] == "logged_out"
    assert r["bridge_login_url"] == "http://bridge:8080/login"
    assert not r["can_replace_key"]


def test_grok_web_rejects_non_http_bridge_url():
    r = _classify(
        "grok-web",
        extra_config={"bridge_url": "javascript:alert(1)"},
        cb=OPEN,
        auth_failure={"since": 1.0, "last_error": "x"},
    )
    assert r["bridge_login_url"] is None


def test_billing_marker_set_and_cleared_by_breaker():
    _run(cb.record_failure("p1", billing_error=True))
    assert cb.get_billing_failure("p1") is not None
    _run(cb.force_close("p1"))
    assert cb.get_billing_failure("p1") is None


def test_build_list_skips_disabled_and_healthy():
    _run(cb.record_failure("p1", billing_error=True))
    _run(cb.record_failure("p2", billing_error=True))
    provs = [
        SimpleNamespace(
            id="p1",
            name="A",
            provider_type="openai",
            enabled=True,
            extra_config={},
            api_key="sk-secret",
        ),
        SimpleNamespace(
            id="p2",
            name="B",
            provider_type="openai",
            enabled=False,
            extra_config={},
            api_key="sk-secret",
        ),
        SimpleNamespace(
            id="p3",
            name="C",
            provider_type="openai",
            enabled=True,
            extra_config={},
            api_key="sk-secret",
        ),
    ]
    rows = att.build_attention_list(provs)
    assert [r["provider_id"] for r in rows] == ["p1"]
    assert "sk-secret" not in repr(rows)


def test_retest_resets_only_on_success(monkeypatch):
    prov = SimpleNamespace(id="p1", api_key="sk-secret")
    _run(cb.record_failure("p1", billing_error=True))

    async def bad(p):
        return {"success": False, "error": "401 invalid"}

    monkeypatch.setattr("app.providers.scanner.test_provider", bad)
    res = _run(att.retest_and_reset(prov))
    assert res == {"success": False, "reset": False, "error": "401 invalid"}
    assert cb.get_all_states()["p1"]["state"] == "open"

    async def good(p):
        return {"success": True, "response": "ok"}

    monkeypatch.setattr("app.providers.scanner.test_provider", good)
    res = _run(att.retest_and_reset(prov))
    assert res == {"success": True, "reset": True, "peers": []}
    assert cb.get_all_states()["p1"]["state"] == "closed"
    assert "sk-secret" not in repr(res)


def test_retest_exception_does_not_reset_or_raise(monkeypatch):
    prov = SimpleNamespace(id="p1", api_key="k")
    _run(cb.record_failure("p1", billing_error=True))

    async def boom(p):
        raise RuntimeError("net down")

    monkeypatch.setattr("app.providers.scanner.test_provider", boom)
    res = _run(att.retest_and_reset(prov))
    assert res["success"] is False and res["reset"] is False
    assert cb.get_all_states()["p1"]["state"] == "open"


def test_routes_are_admin_gated():
    from app.api.provider_attention import router
    from app.auth.admin import require_admin

    for route in router.routes:
        deps = [d.call for d in route.dependant.dependencies]
        assert require_admin in deps, route.path
