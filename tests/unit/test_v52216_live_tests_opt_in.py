"""v5.22.16 — the test suite must not touch production unless asked to.

``tests/conftest.py`` defines ``BASE_URL`` as
``https://www.voipguru.org/llm-proxy2`` — production — and its session
fixtures authenticate there as admin, with several creating and deleting API
keys. So ``pytest tests/unit`` on an operator's machine was quietly writing to
the live deployment. It was not obvious precisely because it *worked*: on a
box that can reach production, those tests pass.

The same fixtures are the stated blocker on CI gating the full suite. On a
clean runner they fail rather than skip — no route to voipguru, no admin
password — so a real regression is indistinguishable from "no deployment
here", and the whole suite stays non-gating.

One fix serves both: require an explicit opt-in and SKIP without it. Unit runs
become self-contained, CI can gate the suite, and writing to production
becomes a deliberate act rather than a side effect of running the tests.
"""

import os
from pathlib import Path

import pytest


def _conftest_src() -> str:
    return Path("tests/conftest.py").read_text()


class TestOptInGate:
    def test_helper_exists_and_the_gate_is_structural(self):
        """v5.22.38 — was ``test_helper_exists_and_is_the_single_gate``, and it
        pinned the literal
        ``LIVE_TESTS_ENABLED = _os.environ.get("LLMPROXY_TEST_LIVE") == "1"``.

        The premise turned out to be wrong: ``require_live_deployment()`` was
        never the single gate, because it is only reached through
        ``_api_session()``. Any test building its own ``requests.Session`` or
        Playwright ``page`` from BASE_URL walked past it — measured 2026-09-28
        as 71 errors and 3 failures against the live deployment from a bare
        ``pytest tests/integration``. This test asserting "single gate" is part
        of why that went unnoticed.

        The gate is now structural: ``pytest_collection_modifyitems`` marks
        everything under tests/integration/ and skips it unless opted in, so a
        test cannot escape by not using a fixture. The helper stays for the
        fixtures, and ``LLMPROXY_TEST_EPHEMERAL=1`` also satisfies the gate
        because a private throwaway instance is a deployment.
        """
        src = _conftest_src()
        assert "def require_live_deployment()" in src
        assert "LIVE_TESTS_ENABLED = _EPHEMERAL_REQUESTED or " in src, (
            "an ephemeral instance must satisfy the gate, or the hermetic "
            "integration run cannot happen"
        )
        # The structural half: collection-time skip by location.
        assert "def pytest_collection_modifyitems" in src
        assert "tests{os.sep}integration{os.sep}" in src.replace('f"', '"'), (
            "the gate must skip tests/integration by LOCATION — a gate that "
            "depends on each test remembering a fixture is not a gate"
        )
        assert "_UNROUTABLE" in src, (
            "BASE_URL must fall back to an unroutable sentinel, not production"
        )

    def test_session_factory_checks_the_gate_before_connecting(self):
        """The check has to come before requests.Session(), or the opt-in is
        decorative."""
        src = _conftest_src()
        body = src.split("def _api_session()", 1)[1]
        gate = body.index("require_live_deployment()")
        connect = body.index("requests.Session()")
        assert gate < connect, "gate runs after the connection is built"

    def test_gate_skips_when_not_opted_in(self, monkeypatch):
        monkeypatch.delenv("LLMPROXY_TEST_LIVE", raising=False)
        import importlib

        import tests.conftest as ct

        importlib.reload(ct)
        with pytest.raises(pytest.skip.Exception):
            ct.require_live_deployment()

    def test_gate_allows_when_opted_in(self, monkeypatch):
        monkeypatch.setenv("LLMPROXY_TEST_LIVE", "1")
        import importlib

        import tests.conftest as ct

        importlib.reload(ct)
        ct.require_live_deployment()  # must not raise

    def test_skip_message_names_the_target(self):
        """An operator who opts in should see which deployment they are about
        to write to."""
        src = _conftest_src()
        assert "LLMPROXY_TEST_BASE_URL" in src
        assert "{BASE_URL}" in src


class TestSessionFinishPurge:
    def test_purge_honours_the_master_switch(self):
        """The purge POSTs to production. It already had its own flag; it must
        also respect the one switch that governs touching the deployment."""
        src = _conftest_src()
        finish = src.split("def pytest_sessionfinish", 1)[1].split("def ", 1)[0]
        assert "LLMPROXY_TEST_PURGE_LIVE" in finish
        assert "LIVE_TESTS_ENABLED" in finish


class TestDefaultIsSafe:
    def test_env_is_not_set_in_this_run(self):
        """Sanity: this suite itself is running in the safe default mode."""
        assert os.environ.get("LLMPROXY_TEST_LIVE") != "1", (
            "this test run has live access enabled — it may be writing to "
            "the production deployment"
        )
