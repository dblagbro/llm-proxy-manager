"""v5.0.17 — session cookie name + path are env-configurable.

The 2026-06-05 clone-fork (operator deployed a snapshot copy of
llm-proxy2 at `/llm-proxy/` alongside the original at `/llm-proxy2/`
on the same domain) hit a cookie collision: both instances issued
``Set-Cookie: llmproxy_session=...; Path=/`` so every login on one
side clobbered the other side's session.

v5.0.17 makes ``SESSION_COOKIE_NAME`` and ``SESSION_COOKIE_PATH``
read from environment, defaulting to the existing values. The clone
runs with ``SESSION_COOKIE_NAME=llmproxy_clone_session`` so the two
instances coexist cleanly.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


# ── Source guard ────────────────────────────────────────────────────


def test_admin_reads_cookie_name_from_env():
    """The constants must be read from environment (with safe defaults)
    so the clone-fork can override without a code change."""
    src = Path("app/auth/admin.py").read_text()
    assert 'os.environ.get("SESSION_COOKIE_NAME"' in src, (
        "SESSION_COOKIE_NAME is no longer env-driven — the clone-fork "
        "pattern needs to override this. Restore the env lookup."
    )
    assert 'os.environ.get("SESSION_COOKIE_PATH"' in src, (
        "SESSION_COOKIE_PATH is no longer env-driven — restore the env "
        "lookup so per-instance scoping works."
    )


# ── Behavioral ──────────────────────────────────────────────────────


def _reimport_admin():
    """Force a fresh import of app.auth.admin so the module-level
    constants re-evaluate against the current env."""
    sys.modules.pop("app.auth.admin", None)
    return importlib.import_module("app.auth.admin")


def test_default_cookie_name_unchanged(monkeypatch):
    """Backward-compat: with no env override, defaults preserve the
    pre-v5.0.17 values exactly (every existing deployment keeps working
    without a compose change)."""
    monkeypatch.delenv("SESSION_COOKIE_NAME", raising=False)
    monkeypatch.delenv("SESSION_COOKIE_PATH", raising=False)
    admin = _reimport_admin()
    assert admin.SESSION_COOKIE_NAME == "llmproxy_session"
    assert admin.SESSION_COOKIE_PATH == "/"


def test_env_overrides_cookie_name(monkeypatch):
    """The clone-fork's compose env override applies."""
    monkeypatch.setenv("SESSION_COOKIE_NAME", "llmproxy_clone_session")
    admin = _reimport_admin()
    assert admin.SESSION_COOKIE_NAME == "llmproxy_clone_session"


def test_env_overrides_cookie_path(monkeypatch):
    """Path scoping override (e.g., for prefix-isolated sessions if a
    future operator wants browser-level isolation in addition to name)."""
    monkeypatch.setenv("SESSION_COOKIE_PATH", "/llm-proxy")
    admin = _reimport_admin()
    assert admin.SESSION_COOKIE_PATH == "/llm-proxy"


def test_legacy_cookie_name_still_recognized(monkeypatch):
    """v2.6.1's legacy ``session`` cookie acceptance must survive the
    refactor — old browser tabs are still in the wild."""
    monkeypatch.delenv("SESSION_COOKIE_NAME", raising=False)
    admin = _reimport_admin()
    assert admin._LEGACY_COOKIE_NAME == "session"
