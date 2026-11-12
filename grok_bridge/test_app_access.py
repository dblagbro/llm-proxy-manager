"""Actual route inventory and side effects, with no browser startup or provider calls."""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bridge_security import BRIDGE_VERSION
from fastapi.testclient import TestClient


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "synthetic-bridge-test-value")
    monkeypatch.setenv("BRIDGE_PUBLIC_PATH", "/grok-bridge")
    monkeypatch.setenv("BRIDGE_PUBLIC_ORIGIN", "https://operator.example.test")
    spec = importlib.util.spec_from_file_location("grok_test_app", Path(__file__).parent / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Deliberately no TestClient context manager: lifespan must not start Chromium.
    client = TestClient(module.app)
    page = AsyncMock()
    page.url = "https://grok.com/"
    module._page = page
    module._context = AsyncMock()
    module._context.cookies.return_value = []
    return module, client, page


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/status", "get"),
        ("/login", "get"),
        ("/api/conversation/new", "post"),
        ("/api/login/start", "post"),
        ("/api/login/import-cookies", "post"),
        ("/api/chat", "post"),
        ("/api/diagnostic/capture_next_send", "post"),
        ("/api/diagnostic/probe_input", "post"),
    ],
)
def test_anonymous_actual_routes_have_no_side_effects(bridge, path, method):
    module, client, page = bridge
    response = getattr(client, method)(path)
    assert response.status_code in (401, 403)
    page.goto.assert_not_called()
    page.evaluate.assert_not_called()
    module._context.cookies.assert_not_called()
    module._context.add_cookies.assert_not_called()


def _admin_session(module, monkeypatch):
    """Same mechanism as test_security.py: stub the admin-session verifier."""
    verifier = AsyncMock()
    monkeypatch.setattr(module.app.state.bridge_access, "admin_session", verifier)
    return verifier


ADMIN_HEADERS = {"Cookie": "synthetic=session", "Origin": "https://operator.example.test"}


def test_token_navigation_executes_only_after_auth(bridge, monkeypatch):
    module, client, page = bridge
    # The bridge token (valid or not) grants nothing on /api/login/start.
    for token in ("incorrect", "synthetic-bridge-test-value"):
        response = client.post("/api/login/start", headers={"X-Bridge-Token": token})
        assert response.status_code in (401, 403)
    page.goto.assert_not_called()
    # Authenticated administrator session + same origin: navigation runs.
    verifier = _admin_session(module, monkeypatch)
    response = client.post("/api/login/start", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    verifier.assert_awaited_once_with("synthetic=session")
    page.goto.assert_awaited_once()


def test_health_version_and_dead_browser(bridge):
    module, client, _ = bridge
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["version"] == module.app.version == BRIDGE_VERSION
    module._context.cookies.side_effect = RuntimeError("synthetic closed context")
    assert client.get("/healthz").status_code == 503


def test_login_html_preserves_subpath_and_hides_token(bridge, monkeypatch):
    module, client, _ = bridge
    # No admin session: the bridge token must not unlock the login page.
    denied = client.get("/login", headers={"X-Bridge-Token": "synthetic-bridge-test-value"})
    assert denied.status_code in (401, 403)
    _admin_session(module, monkeypatch)
    response = client.get(
        "/login", headers={**ADMIN_HEADERS, "X-Bridge-Token": "synthetic-bridge-test-value"}
    )
    assert response.status_code == 200
    assert "synthetic-bridge-test-value" not in response.text
    assert "/grok-bridge/vnc/vnc.html" in response.text
    assert "grok-bridge/vnc/websockify" in response.text
    assert "/grok-bridge/api/login/start" in response.text
