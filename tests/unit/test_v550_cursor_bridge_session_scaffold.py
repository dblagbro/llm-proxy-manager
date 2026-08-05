"""v5.5.0 — cursor_bridge_session sidecar scaffold pin tests.

Phase 1 of the noVNC project. v5.5.0 ships scaffold ONLY: directory +
Dockerfile + supervisord + FastAPI app.py with /healthz live. No
Chromium launch, no /vnc/ route, no compose entry.

Pin contracts:
1. All 5 scaffold files exist.
2. Dockerfile uses the Playwright base image (matching grok_bridge).
3. supervisord.conf names the 4-program stack.
4. app.py exposes /healthz returning the scaffold sentinel.
5. v5.5.1-v5.5.3 stub endpoints are present (so the API surface is
   locked from the start; later ships fill in behavior).
6. Design doc exists at the documented path.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


SCAFFOLD_ROOT = Path("cursor_bridge_session")
SCAFFOLD_FILES = [
    "Dockerfile",
    "requirements.txt",
    "supervisord.conf",
    "start.sh",
    "app.py",
]


def test_scaffold_directory_exists():
    assert SCAFFOLD_ROOT.is_dir(), (
        f"v5.5.0 scaffold dir missing: {SCAFFOLD_ROOT.resolve()}"
    )


def test_all_scaffold_files_present():
    for name in SCAFFOLD_FILES:
        p = SCAFFOLD_ROOT / name
        assert p.is_file(), f"missing scaffold file: {p}"


def test_dockerfile_uses_playwright_base():
    src = (SCAFFOLD_ROOT / "Dockerfile").read_text()
    assert "FROM mcr.microsoft.com/playwright/python" in src, (
        "Dockerfile must use Playwright base image (matching grok_bridge)"
    )


def test_dockerfile_installs_novnc_stack():
    src = (SCAFFOLD_ROOT / "Dockerfile").read_text()
    for pkg in ("xvfb", "x11vnc", "novnc", "websockify", "fluxbox", "supervisor"):
        assert pkg in src, f"Dockerfile missing apt package: {pkg}"


def test_supervisord_names_four_program_stack():
    src = (SCAFFOLD_ROOT / "supervisord.conf").read_text()
    for prog in ("[program:xvfb]", "[program:fluxbox]", "[program:x11vnc]", "[program:websockify]"):
        assert prog in src, f"supervisord.conf missing section: {prog}"


def test_app_py_exposes_required_endpoints():
    src = (SCAFFOLD_ROOT / "app.py").read_text()
    # Live in v5.5.0
    assert "@app.get(\"/healthz\")" in src
    # Stubs locking the API surface for v5.5.1-v5.5.3
    assert "@app.get(\"/api/status\")" in src
    assert "@app.post(\"/api/rotate\")" in src


def test_app_py_version_matches_v550():
    # The cursor-bridge scaffold carries its OWN version series (5.5.x),
    # independent of the main app. Assert it declares a 5.5.x version
    # rather than pinning the exact patch (which advances per ship:
    # 5.5.0 scaffold -> 5.5.1 Playwright -> ...).
    src = (SCAFFOLD_ROOT / "app.py").read_text()
    assert 'version="5.5.' in src


def test_design_doc_exists():
    doc = Path("docs/cursor-oauth-novnc-design-v5.5.md")
    assert doc.is_file(), f"design doc missing: {doc}"
    src = doc.read_text()
    # Pin the phased ship plan so future ships don't accidentally
    # drop a phase.
    for phase in ("v5.5.0", "v5.5.1", "v5.5.2", "v5.5.3"):
        assert phase in src, f"design doc missing phase: {phase}"


def test_healthz_returns_scaffold_sentinel_in_source():
    """Source-grep: /healthz returns the scaffold sentinel shape.
    Import-and-call is fragile because the scaffold's app.py has the
    same module name as the proxy's app/, so a sys.path-insert dance
    clashes with the rest of the suite. Source-grep is the same
    pattern v5.4.0-v5.4.4 uses."""
    src = (SCAFFOLD_ROOT / "app.py").read_text()
    assert '"status": "ok"' in src
    assert '"phase": "scaffold-v5.5.0"' in src
    assert '"uptime_sec"' in src


def test_rotate_endpoint_returns_not_implemented_stub_in_source():
    """v5.5.0 must NOT silently claim rotation works. /api/rotate
    returns a clear error pointing at v5.5.1."""
    src = (SCAFFOLD_ROOT / "app.py").read_text()
    assert '"ok": False' in src
    assert '"not-implemented-in-scaffold"' in src
    assert "v5.5.1" in src
