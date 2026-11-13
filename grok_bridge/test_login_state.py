"""Login-state handling: healthz field, /api/chat fail-fast, refresh-tick warning.
Mocks the browser; no network."""

import asyncio
import importlib.util
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

TOKEN = "synthetic-bridge-test-value"
LOGGED_IN = [{"name": "sso", "value": "v"}, {"name": "x-userid", "value": "v"}]
CHAT = {"messages": [{"role": "user", "content": "hi"}], "conversation_id": "c1"}


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("BRIDGE_PUBLIC_PATH", "/grok-bridge")
    monkeypatch.setenv("BRIDGE_PUBLIC_ORIGIN", "https://operator.example.test")
    path = Path(__file__).parent / "app.py"
    spec = importlib.util.spec_from_file_location("grok_login_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    page = AsyncMock()
    page.url = "https://grok.com/"
    module._page = page
    module._context = AsyncMock()
    module._context.cookies.return_value = []
    return module, TestClient(module.app), page


def test_healthz_reports_logged_out_without_failing(bridge):
    module, client, _ = bridge
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["logged_in"] is False
    module._context.cookies.return_value = LOGGED_IN
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["logged_in"] is True


def test_chat_logged_out_returns_401_before_browser(bridge):
    module, client, page = bridge
    r = client.post("/api/chat", json=CHAT, headers={"X-Bridge-Token": TOKEN})
    assert r.status_code == 401
    assert r.json() == {"error": "grok session logged out"}
    page.goto.assert_not_called()
    page.evaluate.assert_not_called()
    page.query_selector.assert_not_called()


def test_chat_still_requires_bridge_token(bridge):
    _, client, _ = bridge
    r = client.post("/api/chat", json=CHAT)
    assert r.status_code in (401, 403)
    assert "logged out" not in r.text


def test_refresh_tick_warns_when_logged_out(bridge, monkeypatch, caplog):
    module, _, page = bridge
    monkeypatch.setattr(module, "COOKIE_REFRESH_INTERVAL_SEC", 0)
    calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(sec):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    with caplog.at_level(logging.INFO):
        asyncio.run(module._cookie_refresh_loop())
    assert module._last_refresh_status == "logged out"
    assert any(
        r.levelno == logging.WARNING and "logged out" in r.getMessage()
        for r in caplog.records
    )
    assert not any("tick: ok" in r.getMessage() for r in caplog.records)


def test_refresh_tick_ok_when_logged_in(bridge, monkeypatch, caplog):
    module, _, _ = bridge
    module._context.cookies.return_value = LOGGED_IN
    monkeypatch.setattr(module, "COOKIE_REFRESH_INTERVAL_SEC", 0)
    calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(sec):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    with caplog.at_level(logging.INFO):
        asyncio.run(module._cookie_refresh_loop())
    assert module._last_refresh_status == "ok"
