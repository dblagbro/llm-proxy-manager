"""Authenticate browser control at the bridge, including direct Docker traffic."""

import asyncio
import contextlib
import os
import secrets

import httpx
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import HTTPConnection, Request
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

BRIDGE_VERSION = "1.2.0"
TOKEN_ONLY_PATHS = {
    "/api/chat",
    "/api/diagnostic/capture_next_send",
    "/api/diagnostic/probe_input",
    "/api/login/import-cookies",
}


def require_token(expected: str, supplied: str | None) -> None:
    if not expected or not expected.strip():
        raise HTTPException(503, "bridge authentication is not configured")
    if not supplied or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "invalid bridge token")


class BridgeAccess:
    def __init__(self):
        self.token = os.environ.get("BRIDGE_TOKEN", "").strip()
        self.auth_url = os.environ.get("BRIDGE_AUTH_URL", "http://llm-proxy2:3000/api/auth/me")
        self.public_origin = os.environ.get("BRIDGE_PUBLIC_ORIGIN", "").rstrip("/")

    async def admin_session(self, cookie: str) -> None:
        if not cookie:
            raise HTTPException(401, "administrator login required")
        # Fixed operator-configured URL. Never forward authorization assertions,
        # host headers, redirects, or a URL supplied by the requesting client.
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                result = await client.get(self.auth_url, headers={"Cookie": cookie})
            if result.status_code in (401, 403):
                raise HTTPException(result.status_code, "administrator login required")
            if result.status_code != 200:
                raise HTTPException(503, "session verification unavailable")
            identity = result.json()
            if not isinstance(identity, dict) or identity.get("role") != "admin":
                raise HTTPException(403, "administrator role required")
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, "session verification unavailable") from exc

    async def authorize(self, connection: HTTPConnection, *, token_only=False):
        supplied = connection.headers.get("x-bridge-token")
        # Missing configuration must never open either token or browser access.
        if not self.token:
            raise HTTPException(503, "bridge authentication is not configured")
        if supplied is not None or token_only:
            require_token(self.token, supplied)
            return
        is_websocket = connection.scope["type"] == "websocket"
        unsafe = is_websocket or connection.scope.get("method") not in ("GET", "HEAD")
        if unsafe:
            origin = connection.headers.get("origin")
            if not self.public_origin or origin != self.public_origin:
                raise HTTPException(403, "same-origin browser control required")
        await self.admin_session(connection.headers.get("cookie", ""))


def install_security(app, public_path: str, *, static_directory="/usr/share/novnc"):
    access = BridgeAccess()
    app.state.bridge_access = access

    @app.middleware("http")
    async def enforce_access(request: Request, call_next):
        if request.url.path != "/healthz":
            try:
                await access.authorize(request, token_only=request.url.path in TOKEN_ONLY_PATHS)
            except HTTPException as exc:
                return JSONResponse(
                    {"detail": exc.detail},
                    exc.status_code,
                    headers={"Cache-Control": "no-store"},
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/vnc")
    @app.get("/vnc/")
    async def vnc_index():
        ws_path = f"{public_path.lstrip('/')}/vnc/websockify".lstrip("/")
        return RedirectResponse(
            f"{public_path}/vnc/vnc.html?path={ws_path}&autoconnect=true&resize=remote"
        )

    @app.websocket("/vnc/websockify")
    async def vnc_socket(websocket: WebSocket):
        try:
            await access.authorize(websocket)
        except HTTPException:
            await websocket.close(code=1008)
            return
        # The only backend is loopback. URL/path/headers from the client cannot
        # select another destination. Binary is the noVNC/websockify protocol.
        try:
            async with connect(
                "ws://127.0.0.1:6080/websockify",
                subprotocols=["binary"],
                proxy=None,
                open_timeout=3,
                max_size=4 * 1024 * 1024,
            ) as upstream:
                offered = websocket.scope.get("subprotocols", [])
                await websocket.accept(subprotocol="binary" if "binary" in offered else None)

                async def to_backend():
                    while True:
                        await upstream.send(await websocket.receive_bytes())

                async def to_browser():
                    async for data in upstream:
                        if not isinstance(data, bytes):
                            raise TypeError("unexpected VNC text frame")
                        await websocket.send_bytes(data)

                async def verify_session():
                    while True:
                        await asyncio.sleep(30)
                        await access.authorize(websocket)

                tasks = [
                    asyncio.create_task(fn()) for fn in (to_backend, to_browser, verify_session)
                ]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (
            HTTPException,
            WebSocketDisconnect,
            ConnectionClosed,
            OSError,
            TypeError,
            ValueError,
        ):
            pass
        finally:
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=1000)

    # StaticFiles normalizes paths and rejects traversal outside this directory.
    # HTTP middleware above protects every asset; do not expose websockify's HTTP
    # listener through Nginx or Docker ports.
    app.mount("/vnc", StaticFiles(directory=static_directory, check_dir=False))
