"""Offline access-boundary regression tests; no Grok account or network calls."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import bridge_security as security
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

TOKEN = "synthetic-bridge-test-value"
ORIGIN = "https://operator.example.test"


@pytest.fixture
def site(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("BRIDGE_PUBLIC_ORIGIN", ORIGIN)
    (tmp_path / "vnc.html").write_text("synthetic VNC page")
    app = FastAPI()
    calls = []

    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.get("/api/status")
    def status():
        calls.append("status")
        return {"ready": True}

    @app.post("/api/login/start")
    def navigate():
        calls.append("navigate")
        return {"ok": True}

    @app.post("/api/chat")
    def chat():
        calls.append("chat")
        return {"ok": True}

    security.install_security(app, "/grok-bridge", static_directory=str(tmp_path))
    verifier = AsyncMock()
    monkeypatch.setattr(app.state.bridge_access, "admin_session", verifier)
    return TestClient(app), app.state.bridge_access, calls, verifier


@pytest.mark.parametrize(
    "expected,supplied,code",
    [
        ("", None, 503),
        ("   ", TOKEN, 503),
        (TOKEN, None, 401),
        (TOKEN, "", 401),
        (TOKEN, " ", 401),
        (TOKEN, "wrong", 401),
        (TOKEN, TOKEN + " ", 401),
        (TOKEN, "nonascii-\u2603", 401),
    ],
)
def test_token_rejects_invalid(expected, supplied, code):
    with pytest.raises(HTTPException) as err:
        security.require_token(expected, supplied)
    assert err.value.status_code == code


def test_correct_token():
    security.require_token(TOKEN, TOKEN)


def test_health_is_public(site):
    client, access, calls, verifier = site
    access.token = ""
    assert client.get("/healthz").status_code == 200
    verifier.assert_not_called()
    assert not calls


@pytest.mark.parametrize("path", ["/api/status", "/api/chat", "/login", "/vnc/vnc.html"])
def test_missing_configuration_fails_closed(site, path):
    client, access, calls, verifier = site
    access.token = ""
    assert client.get(path).status_code == 503
    verifier.assert_not_called()
    assert not calls


@pytest.mark.parametrize("code", [401, 403, 503])
def test_bad_session_does_not_reach_handler(site, code):
    client, _, calls, verifier = site
    verifier.side_effect = HTTPException(code, "denied")
    assert client.get("/api/status", headers={"Cookie": "synthetic=session"}).status_code == code
    assert not calls


def test_ignores_forged_identity_headers(site):
    client, _, calls, verifier = site
    verifier.side_effect = HTTPException(401, "denied")
    response = client.get(
        "/api/status",
        headers={
            "X-Forwarded-User": "admin",
            "X-Forwarded-Role": "admin",
            "X-Authenticated": "1",
        },
    )
    assert response.status_code == 401
    verifier.assert_awaited_once_with("")
    assert not calls


@pytest.mark.parametrize("origin", [None, "null", "https://attacker.example", ORIGIN + ".evil"])
def test_browser_mutation_rejects_bad_origin(site, origin):
    client, _, calls, verifier = site
    headers = {"Cookie": "synthetic=session"}
    if origin is not None:
        headers["Origin"] = origin
    assert client.post("/api/login/start", headers=headers).status_code == 403
    verifier.assert_not_called()
    assert not calls


def test_admin_browser_navigation_preserves_cookie_workflow(site):
    client, _, calls, verifier = site
    response = client.post(
        "/api/login/start", headers={"Cookie": "synthetic=session", "Origin": ORIGIN}
    )
    assert response.status_code == 200
    assert calls == ["navigate"]
    verifier.assert_awaited_once_with("synthetic=session")
    assert TOKEN not in response.text


def test_machine_token_does_not_need_browser_origin(site):
    client, _, calls, verifier = site
    assert client.post("/api/chat", headers={"X-Bridge-Token": TOKEN}).status_code == 200
    verifier.assert_not_called()
    assert calls == ["chat"]


def test_cookie_cannot_replace_machine_token(site):
    client, _, calls, verifier = site
    assert (
        client.post(
            "/api/chat", headers={"Cookie": "synthetic=session", "Origin": ORIGIN}
        ).status_code
        == 401
    )
    verifier.assert_not_called()
    assert not calls


def test_wrong_token_cannot_fall_back_to_admin_cookie(site):
    client, _, calls, verifier = site
    assert (
        client.get(
            "/api/status",
            headers={"X-Bridge-Token": "wrong", "Cookie": "synthetic=session"},
        ).status_code
        == 401
    )
    verifier.assert_not_called()
    assert not calls


@pytest.mark.parametrize("path", ["/vnc", "/vnc/"])
def test_subpath_redirect_never_uses_localhost(site, path):
    client, _, _, _ = site
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 307
    assert (
        response.headers["location"] == "/grok-bridge/vnc/vnc.html?path=grok-bridge/vnc/websockify"
        "&autoconnect=true&resize=remote"
    )
    assert response.headers["cache-control"] == "no-store"


def test_static_assets_require_session(site):
    client, _, _, verifier = site
    assert client.get("/vnc/vnc.html").text == "synthetic VNC page"
    verifier.side_effect = HTTPException(401, "denied")
    assert client.get("/vnc/vnc.html").status_code == 401


def test_static_traversal_rejected(site):
    client, _, _, _ = site
    assert client.get("/vnc/%2e%2e/%2e%2e/secret.txt").status_code == 404


@pytest.mark.parametrize(
    "headers", [{}, {"Origin": "https://attacker.example"}, {"X-Bridge-Token": "wrong"}]
)
def test_websocket_rejects_before_connect(site, monkeypatch, headers):
    client, _, _, _ = site
    connect = AsyncMock()
    monkeypatch.setattr(security, "connect", connect)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/vnc/websockify", headers=headers),
    ):
        pytest.fail("unauthorized socket opened")
    connect.assert_not_called()


def test_expired_websocket_session_rejected(site, monkeypatch):
    client, _, _, verifier = site
    verifier.side_effect = HTTPException(401, "expired")
    connect = AsyncMock()
    monkeypatch.setattr(security, "connect", connect)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/vnc/websockify", headers={"Origin": ORIGIN, "Cookie": "expired=value"}
        ),
    ):
        pytest.fail("expired socket opened")
    connect.assert_not_called()


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (200, {"role": "admin"}, None),
        (200, {"role": "user"}, 403),
        (200, {}, 403),
        (200, [], 403),
        (302, {}, 503),
        (401, {}, 401),
        (403, {}, 403),
        (500, {}, 503),
    ],
)
def test_auth_service_requires_explicit_admin(monkeypatch, status, payload, expected):
    result = httpx.Response(status, json=payload)
    get = AsyncMock(return_value=result)
    factory = AsyncMock()
    factory.__aenter__.return_value.get = get
    monkeypatch.setattr(security.httpx, "AsyncClient", lambda **kwargs: factory)
    access = security.BridgeAccess()
    if expected:
        with pytest.raises(HTTPException) as err:
            asyncio.run(access.admin_session("synthetic=only-cookie"))
        assert err.value.status_code == expected
    else:
        asyncio.run(access.admin_session("synthetic=only-cookie"))
    get.assert_awaited_once_with(access.auth_url, headers={"Cookie": "synthetic=only-cookie"})


def test_auth_network_error_fails_closed(monkeypatch):
    factory = AsyncMock()
    factory.__aenter__.return_value.get = AsyncMock(side_effect=httpx.ConnectError("offline"))
    monkeypatch.setattr(security.httpx, "AsyncClient", lambda **kwargs: factory)
    with pytest.raises(HTTPException) as err:
        asyncio.run(security.BridgeAccess().admin_session("synthetic=session"))
    assert err.value.status_code == 503


def test_vnc_binds_are_loopback_only():
    conf = (Path(__file__).parent / "supervisord.conf").read_text(encoding="utf-8")
    assert "-nopw -localhost -rfbport 5900" in conf
    assert "127.0.0.1:6080 127.0.0.1:5900" in conf


def test_authenticated_websocket_binary_roundtrip(site, monkeypatch):
    client, _, _, verifier = site
    connection_calls = []
    closed = []

    class Backend:
        async def __aenter__(self):
            self.frames = asyncio.Queue()
            self.frames.put_nowait(b"RFB 003.008\n")
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.frames.get()

        async def send(self, data):
            await self.frames.put(data)

    def connect(url, **kwargs):
        connection_calls.append((url, kwargs))
        return Backend()

    monkeypatch.setattr(security, "connect", connect)
    with client.websocket_connect(
        "/vnc/websockify",
        headers={"Origin": ORIGIN, "Cookie": "synthetic=session"},
        subprotocols=["binary"],
    ) as socket:
        assert socket.accepted_subprotocol == "binary"
        assert socket.receive_bytes() == b"RFB 003.008\n"
        socket.send_bytes(b"synthetic-binary-frame")
        assert socket.receive_bytes() == b"synthetic-binary-frame"
        socket.send_text("unsupported text")
        with pytest.raises(WebSocketDisconnect) as err:
            socket.receive_bytes()
        assert err.value.code == 1003
    verifier.assert_awaited_once_with("synthetic=session")
    assert connection_calls[0][0] == "ws://127.0.0.1:6080/websockify"
    assert connection_calls[0][1]["proxy"] is None
    assert closed == [True]


@pytest.mark.parametrize("code", [401, 403, 503])
def test_active_socket_session_revocation_closes_backend(site, monkeypatch, code):
    client, _, _, verifier = site
    verifier.side_effect = [None, HTTPException(code, "session no longer valid")]
    monkeypatch.setattr(security, "SESSION_RECHECK_SECONDS", 0.01)
    closed = []

    class Backend:
        async def __aenter__(self):
            self.ready = True
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.ready:
                self.ready = False
                return b"RFB 003.008\n"
            await asyncio.Event().wait()

        async def send(self, data):
            pass

    monkeypatch.setattr(security, "connect", lambda *args, **kwargs: Backend())
    with client.websocket_connect(
        "/vnc/websockify",
        headers={"Origin": ORIGIN, "Cookie": "synthetic=session"},
        subprotocols=["binary"],
    ) as socket:
        assert socket.receive_bytes() == b"RFB 003.008\n"
        with pytest.raises(WebSocketDisconnect):
            socket.receive_bytes()
    assert verifier.await_count == 2
    assert closed == [True]
