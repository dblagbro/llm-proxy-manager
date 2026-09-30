"""v5.22.38 — the test suite no longer needs a shared deployment.

Three things had to be true for CI to gate on the suite, and none of them were:

1. ``tests/unit`` makes no outbound connections. It does now, and
   ``tools/run_unit_suite_hermetic.py`` proves it on every run.
2. A test cannot reach a shared deployment by accident. The live gate used to
   live in ``_api_session()``, so it only covered tests using the
   ``admin_session`` fixture; anything building its own ``requests.Session`` or
   Playwright ``page`` from BASE_URL went straight past it. Measured 2026-09-28
   on a bare ``pytest tests/integration``: 78 correctly skipped, 71 errors and 3
   failures against the live deployment. The gate is now applied at collection
   by location.
3. ``tests/integration`` has somewhere to run. It now boots its own throwaway
   instance, so it needs no deployment and no network.

These pins are cheap and the properties are easy to lose one commit at a time.
"""
from __future__ import annotations

import ast
from pathlib import Path

CONFTEST = Path("tests/conftest.py")


def _conftest() -> str:
    return CONFTEST.read_text()


# ── 1. BASE_URL must never default to a shared deployment ────────────────────


def test_base_url_does_not_default_to_production():
    """The old default was https://www.voipguru.org/llm-proxy2 — production.

    Anything that escaped the gate therefore escaped *towards production*. The
    fallback is now an RFC 2606 ``.invalid`` host, which cannot resolve, so the
    worst case is a connection error instead of a write to a live system.
    """
    src = _conftest()
    assert "_UNROUTABLE" in src
    assert ".invalid" in src, "the fallback host must be unresolvable"

    import tests.conftest as ct
    # Whatever this run is configured for, the sentinel itself must be unroutable.
    assert ct._UNROUTABLE.endswith("/llm-proxy2")
    assert ".invalid" in ct._UNROUTABLE
    assert "voipguru" not in ct._UNROUTABLE


def test_no_module_level_production_default_anywhere_in_tests():
    """No test module may hardcode a shared deployment as its own default.

    Five did: test_playwright_ui and test_manual_override_flow defaulted to
    production, and the three UI-pin files hardcoded the smoke instance *and*
    ``ADMIN_PASS = "admin"``. All five now import BASE_URL from tests.conftest,
    so ``LLMPROXY_TEST_EPHEMERAL=1`` reaches them too.
    """
    offenders = []
    for path in sorted(Path("tests").rglob("*.py")):
        if path.name == "conftest.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Assign):
                continue
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if not names & {"BASE_URL", "BASE"}:
                continue
            literals = [
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            if any("voipguru" in s for s in literals):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        "these hardcode a shared deployment as their target: "
        f"{offenders}. Import BASE_URL from tests.conftest instead."
    )


def test_no_hardcoded_admin_password_in_tests():
    """v4.4.29's lesson, learned a third time.

    The password left source in v4.4.29, then test_playwright_ui.py was found
    still carrying it on 2026-08-12. Three UI-pin files were missed both times.
    """
    offenders = []
    for path in sorted(Path("tests").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id in ("ADMIN_PASS", "ADMIN_PASSWORD")
                for t in node.targets
            ):
                continue
            # A bare string literal is hardcoded; os.environ.get(...) is not.
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        f"hardcoded admin password at {offenders} — read it from "
        "LLMPROXY_TEST_ADMIN_PASS, or import ADMIN_PASS from tests.conftest"
    )


# ── 2. The gate is structural, not per-fixture ────────────────────────────────


def test_gate_is_applied_at_collection_by_location():
    src = _conftest()
    assert "def pytest_collection_modifyitems" in src
    hook = src.split("def pytest_collection_modifyitems", 1)[1]
    assert "tests" in hook and "integration" in hook, (
        "the gate must skip tests/integration by LOCATION. A gate reached only "
        "through a fixture is one a test can bypass by not using it — which is "
        "exactly what 74 tests did."
    )
    assert "live_deployment" in hook


def test_ephemeral_mode_satisfies_the_gate():
    """A private throwaway instance is a deployment, so it must not be gated
    behind 'are you sure you want to touch production'."""
    src = _conftest()
    assert "LIVE_TESTS_ENABLED = _EPHEMERAL_REQUESTED or " in src


def test_shared_deployment_marker_is_separate_from_live():
    """Tests needing the shared nginx estate (v1 proxy, coordinator hub,
    paperless) cannot be served by an ephemeral instance, so they need their own
    marker rather than being lumped in with 'needs a deployment'."""
    src = _conftest()
    assert "shared_deployment" in src
    assert "LIVE_TARGET_IS_SHARED" in src


def test_tombstone_purge_never_runs_against_an_ephemeral_instance():
    """Purging tombstones in a DB that is seconds from deletion is waste; doing
    it to someone else's deployment is worse."""
    src = _conftest()
    purge = src.split("def pytest_sessionfinish", 1)[1]
    assert "LIVE_TARGET_IS_SHARED" in purge


# ── 3. The ephemeral harness ─────────────────────────────────────────────────


def test_ephemeral_harness_exists_and_is_self_contained():
    src = Path("tests/_ephemeral.py").read_text()
    # A private instance must not join a cluster: empty DB + unset HMAC key.
    assert 'env["CLUSTER_ENABLED"] = "false"' in src
    # Temp DB, not the developer's or the container's.
    assert "tempfile.mkdtemp" in src and "DATABASE_URL" in src
    # Bound to loopback only.
    assert '"--host", "127.0.0.1"' in src
    # Cleaned up even if the run dies.
    assert "atexit.register" in src and "shutil.rmtree" in src


def test_ephemeral_relaxes_the_cookie_only_for_itself():
    """The Secure flag is why this suite could never run locally: over plain
    http, neither requests nor a browser returns a Secure cookie, so login
    succeeded and every authenticated call afterwards 401'd.

    The app must still default to Secure — only the harness may relax it.
    """
    assert 'env["SESSION_COOKIE_SECURE"] = "false"' in Path("tests/_ephemeral.py").read_text()

    import os

    from app.auth.admin import SESSION_COOKIE_SECURE
    if os.environ.get("SESSION_COOKIE_SECURE") is None:
        assert SESSION_COOKIE_SECURE is True, (
            "the session cookie must be Secure by default — every real "
            "deployment is HTTPS and this flag protects the admin session"
        )

    src = Path("app/api/auth.py").read_text()
    assert "secure=SESSION_COOKIE_SECURE," in src
    assert "secure=True," not in src, (
        "a hardcoded secure=True is what made the suite un-runnable locally"
    )


def test_ephemeral_run_provisions_a_provider():
    """A freshly booted instance has an empty providers table, so any test that
    calls an LLM endpoint gets 503 'No providers configured'. 14 of 66 did."""
    src = _conftest()
    assert "_ephemeral_default_provider" in src
    assert "mock_server" in src.split("_ephemeral_default_provider", 1)[1]


# ── The triage list is a triage list ─────────────────────────────────────────


def test_known_integration_failures_is_documented_and_bounded():
    """Making the suite runnable surfaced 13 failures nobody could have seen
    before. They are recorded with causes, not parked silently — the unit
    suite's list reached 75 by being treated as a parking space."""
    path = Path("tests/known_integration_failures.txt")
    assert path.is_file()
    text = path.read_text()
    entries = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert entries, "if it is empty, delete the file and the CI deselect with it"
    assert len(entries) <= 13, (
        f"{len(entries)} entries — this list is meant to shrink. Adding to it "
        "needs a recorded reason; see the file header."
    )
    assert "TRIAGE LIST" in text
    for e in entries:
        assert e.startswith("tests/integration/") and "::" in e, f"malformed: {e}"
