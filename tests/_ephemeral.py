"""Boot a private, throwaway llm-proxy instance for the integration suite.

v5.22.38 — the integration suite had no target but a shared deployment. Every
fixture in ``tests/conftest.py`` pointed at
``https://www.voipguru.org/llm-proxy2`` (production) by default, so the suite
could only run somewhere that URL resolves, could not run on a clean CI runner
at all, and wrote to a deployment other people use.

None of that was necessary. The app boots standalone against a throwaway SQLite
file in a couple of seconds, creates ``admin``/``admin`` on first boot when no
users exist, and serves the built frontend from ``frontend/dist`` — so an
integration run can own its own deployment for the length of the session and
delete it afterwards.

Usage: set ``LLMPROXY_TEST_EPHEMERAL=1``. ``tests/conftest.py`` then boots one
at collection time and points ``BASE_URL`` at it. Nothing else changes: the
fixtures, the Playwright tests and the ``requests``-based tests all just see a
different base URL.

Deliberately booted at import time rather than from a fixture, because
``BASE_URL`` is a module-level constant that nine test modules import directly
(``from tests.conftest import BASE_URL``). A fixture would resolve too late.
That is why this is opt-in per-run rather than automatic — a plain
``pytest tests/unit`` must not pay for a boot it never uses.
"""
from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# Generous: a cold start imports litellm, builds the provider scanner and runs
# create_all. On a loaded CI runner 60s is not excessive.
_BOOT_TIMEOUT_SEC = 60.0
_POLL_INTERVAL_SEC = 0.25


class EphemeralDeployment:
    """A uvicorn process serving app.main against a temporary SQLite file."""

    def __init__(self) -> None:
        self.tmpdir: str | None = None
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.log_path: str | None = None

    @property
    def base_url(self) -> str:
        assert self.port is not None, "not started"
        return f"http://127.0.0.1:{self.port}"

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def start(self) -> EphemeralDeployment:
        self.tmpdir = tempfile.mkdtemp(prefix="llmproxy-ephemeral-")
        self.port = self._free_port()
        self.log_path = os.path.join(self.tmpdir, "uvicorn.log")

        env = dict(os.environ)
        env["DATABASE_URL"] = f"sqlite+aiosqlite:///{self.tmpdir}/ephemeral.db"
        # A private instance must never join a cluster or talk to peers: its
        # HMAC key is unset and its DB is empty, so a sync push would either be
        # rejected or — worse — propagate empty state. See app/cluster/auth.py.
        env["CLUSTER_ENABLED"] = "false"
        env["LLMPROXY_EPHEMERAL"] = "1"
        # Keep the test instance from reaching upstream providers at boot.
        env.setdefault("KEEPALIVE_ENABLED", "false")
        # The session cookie is Secure in every real deployment (all HTTPS).
        # Over plain http://127.0.0.1 neither `requests` nor a browser will send
        # a Secure cookie back, so login returned 200 and every authenticated
        # call after it returned 401 — which is precisely why this suite had no
        # target but a shared HTTPS deployment. Relaxed HERE ONLY: the app
        # defaults to Secure and nothing in compose sets this.
        env["SESSION_COOKIE_SECURE"] = "false"
        env.setdefault("SESSION_COOKIE_PATH", "/")

        with open(self.log_path, "w") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", str(self.port),
                 "--log-level", "warning"],
                stdout=log, stderr=subprocess.STDOUT, env=env,
            )
        atexit.register(self.stop)

        deadline = time.monotonic() + _BOOT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"ephemeral instance exited with {self.proc.returncode} "
                    f"before serving /health:\n{self._tail_log()}"
                )
            try:
                with urllib.request.urlopen(f"{self.base_url}/health", timeout=2):
                    return self
            except (urllib.error.URLError, OSError):
                time.sleep(_POLL_INTERVAL_SEC)

        self.stop()
        raise RuntimeError(
            f"ephemeral instance did not serve /health within "
            f"{_BOOT_TIMEOUT_SEC:.0f}s:\n{self._tail_log()}"
        )

    def _tail_log(self, lines: int = 40) -> str:
        if not self.log_path or not os.path.exists(self.log_path):
            return "(no log)"
        with open(self.log_path) as f:
            return "".join(f.readlines()[-lines:])

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None
        if self.tmpdir and os.path.isdir(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None


_INSTANCE: EphemeralDeployment | None = None


def start_ephemeral() -> str:
    """Boot the shared ephemeral instance (idempotent) and return its base URL."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = EphemeralDeployment().start()
    return _INSTANCE.base_url


def stop_ephemeral() -> None:
    global _INSTANCE
    if _INSTANCE is not None:
        _INSTANCE.stop()
        _INSTANCE = None


def ephemeral_log_tail(lines: int = 40) -> str:
    return _INSTANCE._tail_log(lines) if _INSTANCE else "(not started)"
