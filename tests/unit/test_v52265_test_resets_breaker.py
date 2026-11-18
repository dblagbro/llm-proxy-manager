"""v5.22.65: a passing provider Test clears this node's breaker; a failing one does not."""

import asyncio
from types import SimpleNamespace

import app.api.provider_lifecycle as pl
import app.routing.circuit_breaker as cb


def _run(result, monkeypatch):
    calls = {"close": 0, "clear": 0}

    async def fake_get(db, pid):
        return SimpleNamespace(id=pid, name="p", provider_type="openai", default_model="gpt-x")

    async def fake_test(p):
        return result

    async def fake_close(pid):
        calls["close"] += 1

    def fake_clear(pid):
        calls["clear"] += 1

    monkeypatch.setattr(pl, "_get_or_404", fake_get)
    monkeypatch.setattr(pl, "test_provider", fake_test)
    monkeypatch.setattr(cb, "force_close", fake_close)
    monkeypatch.setattr(cb, "clear_auth_failure", fake_clear)
    out = asyncio.run(pl.test_provider_endpoint("pid", db=None, _=None))
    return out, calls


def test_success_resets_breaker(monkeypatch):
    out, calls = _run({"success": True, "model": "gpt-x"}, monkeypatch)
    assert calls == {"close": 1, "clear": 1}
    assert out["breaker_reset"] is True


def test_failure_keeps_breaker(monkeypatch):
    out, calls = _run({"success": False, "error": "spend limit"}, monkeypatch)
    assert calls == {"close": 0, "clear": 0}
    assert "breaker_reset" not in out
