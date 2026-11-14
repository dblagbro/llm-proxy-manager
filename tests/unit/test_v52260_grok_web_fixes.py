"""v5.22.60: grok-web fallback skip, probe timeout, provider-test ok flag,
bridge 401 "logged out" surfacing. No network, no browser."""
import inspect
import sys
import types

import httpx
import pytest

sys.modules.setdefault("litellm", types.ModuleType("litellm"))

from app.monitoring import keepalive  # noqa: E402
from app.routing import fallback  # noqa: E402
from app.routing.fallback import try_ranked_non_streaming  # noqa: E402


class _P:
    def __init__(self, pid, ptype):
        self.id = pid
        self.name = pid
        self.provider_type = ptype


class _R:
    def __init__(self, pid, ptype):
        self.provider = _P(pid, ptype)
        self.litellm_model = f"{ptype}/m"


# ── A: own-dispatcher types are skipped by the litellm fallback chain ──────

@pytest.mark.asyncio
@pytest.mark.parametrize("ptype", ["grok-web", "claude-oauth", "ChatGPT-oauth-plan"])
async def test_primary_own_dispatcher_never_reaches_call_fn(monkeypatch, ptype):
    good = _R("good", "openai")

    async def fake_next(db, hint, *, tried_ids, **kw):
        return good

    monkeypatch.setattr(fallback, "_next_route", fake_next)
    seen = []

    async def call_fn(route):
        seen.append(route.provider.id)
        return "resp"

    result, route, chain = await try_ranked_non_streaming(
        db=None, hint=None, has_tools=False, has_images=False,
        key_type="standard", pinned_provider_id=None, model_override=None,
        primary_route=_R("prim", ptype), call_fn=call_fn,
    )
    assert seen == ["good"]
    assert route is good
    assert chain.as_header().startswith("prim:skip:oauth-not-via-litellm")


@pytest.mark.asyncio
async def test_next_route_skips_grok_web_candidate(monkeypatch):
    picks = iter([_R("gw", "grok-web"), _R("ok", "openai")])
    excluded_seen = []

    async def fake_select(db, hint, **kw):
        excluded_seen.append(set(kw["exclude_provider_ids"]))
        return next(picks)

    monkeypatch.setattr(fallback, "select_provider", fake_select)
    cand = await fallback._next_route(
        None, None, has_tools=False, has_images=False, key_type="standard",
        pinned_provider_id=None, model_override=None, tried_ids={"prim"},
    )
    assert cand.provider.id == "ok"
    assert "gw" in excluded_seen[1]


# ── B: per-type probe timeout ─────────────────────────────────────────────

def test_probe_timeout_per_type():
    assert keepalive._probe_timeout_sec("grok-web") == 60
    assert keepalive._probe_timeout_sec("openai") == keepalive._PROBE_TIMEOUT_SEC == 15
    assert keepalive._probe_timeout_sec("claude-oauth") == 15


def test_grok_probe_branch_uses_override():
    src = inspect.getsource(keepalive._probe_one)
    grok = src.split('provider.provider_type == "grok-web"', 1)[1][:1800]
    assert '_probe_timeout_sec("grok-web")' in grok
    assert "_PROBE_TIMEOUT_SEC" not in grok


# ── D: provider_test ok flag ──────────────────────────────────────────────

def test_test_result_ok_interpretation():
    from app.api.provider_lifecycle import _test_result_ok
    assert _test_result_ok({"success": False, "error": "invalid bridge token"}) is False
    assert _test_result_ok({"success": True, "response": "hi"}) is True
    assert _test_result_ok({"ok": False}) is False
    assert _test_result_ok({"ok": True}) is True
    assert _test_result_ok({"error": "boom"}) is False
    assert _test_result_ok({"model": "x"}) is True


@pytest.mark.asyncio
async def test_grok_web_test_reports_failure_on_bridge_error_payload(monkeypatch):
    """Bridge 401 'invalid bridge token' -> _test_grok_web success False."""
    from app.providers import scanner
    from app.routing import circuit_breaker

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(circuit_breaker, "record_failure", noop)
    monkeypatch.setattr(circuit_breaker, "record_success", noop)

    class _Resp:
        status_code = 401
        text = '{"detail":"invalid bridge token"}'

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    prov = types.SimpleNamespace(
        id="p1", name="grok", provider_type="grok-web", default_model="grok-3",
        extra_config={"bridge_url": "http://bridge.invalid", "bridge_token": "x",
                      "conversation_id": "c"},
    )
    from app.api.provider_lifecycle import _test_result_ok
    out = await scanner._test_grok_web(prov)
    assert out["success"] is False
    assert "invalid bridge token" in out["error"]
    assert _test_result_ok(out) is False


# ── C (proxy side): bridge 401 logged out message ─────────────────────────

@pytest.mark.asyncio
async def test_bridge_401_logged_out_is_clear(monkeypatch):
    from app.providers import grok_web_bridge
    from app.providers.grok_web import GrokWebAuthError

    class _Resp:
        status_code = 401
        text = '{"error":"grok session logged out"}'

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(grok_web_bridge.httpx, "AsyncClient", _Client)
    with pytest.raises(GrokWebAuthError) as ei:
        await grok_web_bridge._bridge_chat(
            {"bridge_url": "http://bridge.invalid", "conversation_id": "c"},
            [{"role": "user", "content": "hi"}], "grok-3", False, 5.0,
        )
    assert "logged out of grok.com" in str(ei.value)
