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


def test_healthz_reports_liveness_and_playwright_readiness_in_source():
    """v5.22.35 — was ``test_healthz_returns_scaffold_sentinel_in_source``.

    It asserted ``"phase": "scaffold-v5.5.0"``. v5.5.1 (2026-07-02) shipped the
    real Playwright lifecycle and /healthz now reports
    ``"phase": "v5.5.1 (playwright + pkce drive)"`` plus a live
    ``playwright_ready`` flag. The old assertion pinned the scaffold sentinel,
    so it could only pass while the feature was unbuilt — it went red on
    delivery and sat in known_failures.txt.

    Re-pointed at the current contract: /healthz stays cheap (no awaiting the
    browser) and reports both liveness and whether the browser context is up,
    which is what the compose healthcheck and the operator read.

    Source-grep rather than import-and-call: the bridge's ``app.py`` shares a
    module name with the proxy's ``app/`` package, so a sys.path insert here
    clashes with the rest of the suite.
    """
    src = (SCAFFOLD_ROOT / "app.py").read_text()

    # Scope to the healthz function. Grepping the whole module cannot tell
    # /healthz from /api/status, which reports several of the same keys —
    # verified by deleting playwright_ready from /healthz alone: the
    # module-wide assertion still passed because /api/status carries it.
    import ast

    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "healthz"
    )
    healthz = "\n".join(src.splitlines()[fn.lineno - 1 : fn.end_lineno])

    assert '"status": "ok"' in healthz
    assert '"uptime_sec"' in healthz
    assert '"playwright_ready"' in healthz, (
        "/healthz must expose whether the Playwright context is up; without it "
        "a bridge that booted but never got a browser looks healthy"
    )
    assert '"phase": "scaffold-v5.5.0"' not in src, (
        "the v5.5.0 scaffold sentinel is back in /healthz — rotation is "
        "implemented as of v5.5.1 and must not advertise itself as a stub"
    )


def test_rotate_endpoint_drives_pkce_under_a_lock_in_source():
    """v5.22.35 — was ``test_rotate_endpoint_returns_not_implemented_stub_in_source``.

    That test existed so v5.5.0 could not *silently claim rotation works*: it
    pinned ``"ok": False`` and ``"not-implemented-in-scaffold"``. v5.5.1
    implemented rotation, which made the honesty check obsolete by succeeding —
    it went red and was parked.

    The obligation it encoded is now the mirror image: /api/rotate must really
    drive the flow, and must serialize, because concurrent operator clicks (or
    an operator racing the cron trigger) would otherwise stampede a single
    browser context.
    """
    src = (SCAFFOLD_ROOT / "app.py").read_text()
    assert '"not-implemented-in-scaffold"' not in src, (
        "rotation has been implemented since v5.5.1; the not-implemented stub "
        "must not come back without this test being revisited"
    )
    assert "_drive_pkce_once()" in src, (
        "/api/rotate must drive the real PKCE flow"
    )
    assert "async with _rotate_lock:" in src, (
        "rotation must be serialized — concurrent callers would stampede the "
        "single Playwright context"
    )
