"""
llm-proxy2-capture — sidecar PTY runner.

Accepts commands from the main llm-proxy2 container (same docker network,
no auth — the main proxy is the trust boundary), spawns a CLI in a PTY,
pipes the PTY I/O over a WebSocket.

Endpoints:
    POST /spawn            { session_id, command, env } → { ok: true }
                           session_id is opaque; main proxy generates it
                           command is a shell string but only the first
                             word is used as argv[0] — we NEVER pass a
                             shell, and argv[0] must be in CLI_WHITELIST
    WS   /term/{sid}       bidirectional PTY pipe
                             browser→PTY: text frames go to stdin
                             browser→PTY: binary frames are {type:"resize",
                               cols, rows} JSON (so resize doesn't interfere
                               with normal input)
                             PTY→browser: text frames carry raw stdout bytes
                               (caller runs them through xterm.js)
    POST /kill/{sid}       terminate session
    GET  /health           simple liveness

Only ONE concurrent session is allowed. A second /spawn call rejects with
409 unless ?takeover=1 is set (main proxy decides based on admin intent).

Session dies after 15 min of no WebSocket traffic. A killed PTY sends
SIGHUP to the CLI and frees its row in SESSIONS.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from aiohttp import web, WSMsgType
from ptyprocess import PtyProcessUnicode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("capture-runner")


# ── Policy ──────────────────────────────────────────────────────────────────
# argv[0] must match one of these exact binary names. We do NOT run a shell.
CLI_WHITELIST = frozenset({
    "claude",     # @anthropic-ai/claude-code (v2.6.0)
    # Future releases add more: "codex", "cursor", "gh", "gcloud", "az", …
})

IDLE_TIMEOUT_SEC = 15 * 60      # kill PTY after 15 min of no WS traffic
SPAWN_MAX_COLS = 200            # cap resize values
SPAWN_MAX_ROWS = 80


# ── State ───────────────────────────────────────────────────────────────────
@dataclass
class Session:
    session_id: str
    command_argv: list[str]
    env: dict[str, str]
    pty: Optional[PtyProcessUnicode] = None
    last_activity: float = field(default_factory=time.time)
    ws_attached: bool = False

    def touch(self) -> None:
        self.last_activity = time.time()


SESSIONS: dict[str, Session] = {}
_session_lock = asyncio.Lock()


# ── Helpers ─────────────────────────────────────────────────────────────────
def _validate_command(command: str) -> list[str]:
    """Split the command and verify argv[0] is whitelisted.

    Raises ValueError if not.
    """
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise ValueError(f"Invalid command syntax: {e}")
    if not argv:
        raise ValueError("Empty command")
    if argv[0] not in CLI_WHITELIST:
        raise ValueError(
            f"Command {argv[0]!r} not in whitelist: {sorted(CLI_WHITELIST)}"
        )
    return argv


async def _spawn_pty(argv: list[str], env: dict[str, str]) -> PtyProcessUnicode:
    """Fork a PTY running argv. Runs in a thread so we don't block the loop."""
    # Merge supplied env with baseline (so the CLI inherits PATH, TERM, etc.)
    full_env = {**os.environ, **env}

    def _fork():
        return PtyProcessUnicode.spawn(
            argv,
            env=full_env,
            dimensions=(30, 120),  # rows, cols; client may resize
        )

    return await asyncio.get_event_loop().run_in_executor(None, _fork)


async def _idle_sweeper():
    """Kill sessions that have been idle past IDLE_TIMEOUT_SEC."""
    while True:
        await asyncio.sleep(30)
        now = time.time()
        async with _session_lock:
            stale = [
                sid for sid, s in SESSIONS.items()
                if now - s.last_activity > IDLE_TIMEOUT_SEC
            ]
        for sid in stale:
            log.info("idle_timeout sid=%s", sid[:8])
            await _kill_session(sid)


async def _kill_session(sid: str) -> bool:
    async with _session_lock:
        s = SESSIONS.pop(sid, None)
    if s is None:
        return False
    if s.pty is not None:
        try:
            s.pty.kill(signal.SIGHUP)
        except Exception as e:
            log.warning("kill_session.sighup_failed sid=%s err=%s", sid[:8], e)
        try:
            s.pty.close(force=True)
        except Exception:
            pass
    return True


# ── HTTP + WS routes ────────────────────────────────────────────────────────
async def handle_health(_req: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "active_sessions": len(SESSIONS),
        "cli_whitelist": sorted(CLI_WHITELIST),
    })


async def handle_spawn(req: web.Request) -> web.Response:
    body = await req.json()
    session_id = body.get("session_id") or uuid.uuid4().hex
    command = body.get("command", "")
    env = body.get("env", {}) or {}
    takeover = req.query.get("takeover") == "1"

    try:
        argv = _validate_command(command)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    async with _session_lock:
        active = [s for s in SESSIONS.values() if s.pty and s.pty.isalive()]
        if active and not takeover:
            return web.json_response(
                {
                    "error": "session_in_use",
                    "active_session_id": active[0].session_id,
                    "age_seconds": int(time.time() - active[0].last_activity),
                },
                status=409,
            )

    # If takeover: kill any existing first
    if takeover:
        for s in list(SESSIONS.values()):
            await _kill_session(s.session_id)

    try:
        pty = await _spawn_pty(argv, env)
    except Exception as e:
        log.error("spawn_failed argv=%s err=%s", argv, e)
        return web.json_response({"error": f"spawn_failed: {e}"}, status=500)

    async with _session_lock:
        SESSIONS[session_id] = Session(
            session_id=session_id, command_argv=argv, env=env, pty=pty,
        )
    log.info("spawn sid=%s argv=%s", session_id[:8], argv)
    return web.json_response({"ok": True, "session_id": session_id})


async def handle_kill(req: web.Request) -> web.Response:
    sid = req.match_info["sid"]
    killed = await _kill_session(sid)
    return web.json_response({"ok": True, "killed": killed})


async def handle_term_ws(req: web.Request) -> web.WebSocketResponse:
    sid = req.match_info["sid"]
    async with _session_lock:
        session = SESSIONS.get(sid)
    if session is None or session.pty is None:
        return web.Response(status=404, text="session not found")
    if session.ws_attached:
        return web.Response(status=409, text="ws already attached")
    session.ws_attached = True
    session.touch()

    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(req)
    log.info("ws_attach sid=%s", sid[:8])

    # Reader task: PTY → WS
    async def _reader():
        loop = asyncio.get_event_loop()
        while session.pty and session.pty.isalive():
            try:
                chunk = await loop.run_in_executor(None, _read_chunk, session.pty)
            except Exception as e:
                log.info("reader_eof sid=%s err=%s", sid[:8], e)
                break
            if not chunk:
                await asyncio.sleep(0.02)
                continue
            session.touch()
            try:
                await ws.send_str(chunk)
            except ConnectionResetError:
                break
        # Notify exit to the browser, then close
        try:
            code = -1
            if session.pty:
                try:
                    code = session.pty.exitstatus if session.pty.exitstatus is not None else -1
                except Exception:
                    code = -1
            await ws.send_str(json.dumps({"__event__": "exit", "code": code}))
        except Exception:
            pass
        await ws.close()

    reader_task = asyncio.create_task(_reader())

    # Writer: WS → PTY
    try:
        async for msg in ws:
            session.touch()
            if msg.type == WSMsgType.TEXT:
                data = msg.data
                # Control messages (resize) come as JSON starting with a brace
                if data.startswith("{") and data.rstrip().endswith("}"):
                    try:
                        parsed = json.loads(data)
                        if isinstance(parsed, dict) and parsed.get("type") == "resize":
                            cols = min(int(parsed.get("cols", 80)), SPAWN_MAX_COLS)
                            rows = min(int(parsed.get("rows", 24)), SPAWN_MAX_ROWS)
                            try:
                                session.pty.setwinsize(rows, cols)
                            except Exception as e:
                                log.debug("resize_failed %s", e)
                            continue
                    except (ValueError, TypeError):
                        pass  # fall through to stdin write
                try:
                    session.pty.write(data)
                except Exception as e:
                    log.info("write_failed sid=%s err=%s", sid[:8], e)
                    break
            elif msg.type == WSMsgType.ERROR:
                log.info("ws_error sid=%s err=%s", sid[:8], ws.exception())
                break
    finally:
        reader_task.cancel()
        # Don't auto-kill the session on WS drop — browser might reconnect.
        # Idle sweeper will reap it if nothing comes in for 15 min.
        session.ws_attached = False
        log.info("ws_detach sid=%s", sid[:8])

    return ws


def _read_chunk(pty: PtyProcessUnicode) -> str:
    """Non-blocking(ish) PTY read. Returns "" when nothing's ready."""
    try:
        return pty.read(4096)
    except EOFError:
        raise
    except Exception:
        return ""


# ── App bootstrap ───────────────────────────────────────────────────────────
async def _start_background(app: web.Application) -> None:
    app["idle_sweeper"] = asyncio.create_task(_idle_sweeper())


async def _stop_background(app: web.Application) -> None:
    task = app.get("idle_sweeper")
    if task:
        task.cancel()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/spawn", handle_spawn)
    app.router.add_post("/kill/{sid}", handle_kill)
    app.router.add_get("/term/{sid}", handle_term_ws)
    app.on_startup.append(_start_background)
    app.on_cleanup.append(_stop_background)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=4000)
