"""v5.22.12 — no working credential may sit in source on a PUBLIC repo.

github.com/dblagbro/llm-proxy-manager is public. Verified 2026-08-12 by
fetching tests/integration/test_playwright_ui.py anonymously from
raw.githubusercontent.com: it returned the live admin password in plaintext.

Two real exposures were found and fixed:

  1. The admin password. tests/conftest.py had been moved to an env var in
     v4.4.29, but the two INTEGRATION test files kept the literal, so the
     credential stayed published. The guard added in v4.4.29 only covered
     conftest.py, which is why the regression went unnoticed for months —
     hence this file checks every candidate, not one path.

  2. `DEFAULT_BRIDGE_TOKEN` held a hardcoded literal in the grok provider
     form. That was the token the bridge actually enforced; sending it to
     the internet-facing /grok-bridge/api/chat returned 400 (bad body)
     rather than 401 (bad token), confirming it authenticated. Anyone
     reading the repo could drive the operator's logged-in grok.com
     session.

Rotating the values is the operator's job; this file stops them coming back.
"""
import re
from pathlib import Path

import pytest

# The specific string that leaked. Kept here deliberately: this is the one
# place it may appear, precisely so it can be asserted absent everywhere else.
_LEAKED_PASSWORD = "Super" + "*120120"
_LEAKED_BRIDGE_TOKEN = "bridge-internal" + "-2026"

_SEARCH_ROOTS = ("app", "tests", "frontend/src", "sdk", "scripts")
_SKIP_SUFFIXES = (".pyc", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".map")


def _source_files():
    for root in _SEARCH_ROOTS:
        base = Path(root)
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not p.name.endswith(_SKIP_SUFFIXES):
                if "node_modules" in p.parts or "__pycache__" in p.parts:
                    continue
                yield p


class TestNoLeakedCredentialsAnywhere:
    def test_admin_password_absent_from_all_source(self):
        offenders = []
        for p in _source_files():
            if p.name == Path(__file__).name:
                continue
            try:
                if _LEAKED_PASSWORD in p.read_text(encoding="utf-8", errors="ignore"):
                    offenders.append(str(p))
            except Exception:
                continue
        # the v4.4.29 guard legitimately names it to assert its absence
        offenders = [o for o in offenders
                     if not o.endswith("test_v4429_cache_marker_overcap_log.py")]
        assert not offenders, f"leaked admin password present in: {offenders}"

    def test_bridge_token_absent_from_all_source(self):
        offenders = [
            str(p) for p in _source_files()
            if p.name != Path(__file__).name
            and _LEAKED_BRIDGE_TOKEN in p.read_text(encoding="utf-8", errors="ignore")
        ]
        assert not offenders, f"leaked bridge token present in: {offenders}"


@pytest.mark.parametrize("path", [
    "tests/conftest.py",
    "tests/integration/test_playwright_ui.py",
    "tests/integration/test_manual_override_flow.py",
])
class TestCredentialsComeFromTheEnvironment:
    def test_reads_password_from_env(self, path):
        src = Path(path).read_text(encoding="utf-8")
        assert "LLMPROXY_TEST_ADMIN_PASS" in src, (
            f"{path} must read the admin password from the environment"
        )

    def test_no_quoted_password_assignment(self, path):
        """ADMIN_PASS must never be assigned a string literal again."""
        src = Path(path).read_text(encoding="utf-8")
        bad = re.findall(r'ADMIN_PASS\s*=\s*["\'][^"\']+["\']', src)
        assert not bad, f"{path} assigns a literal password: {bad}"


class TestBridgeTokenIsOperatorSupplied:
    def test_default_is_blank(self):
        src = Path(
            "frontend/src/components/providers/GrokWebProviderFields.tsx"
        ).read_text(encoding="utf-8")
        m = re.search(r"const DEFAULT_BRIDGE_TOKEN\s*=\s*'([^']*)'", src)
        assert m, "DEFAULT_BRIDGE_TOKEN declaration not found"
        assert m.group(1) == "", (
            "the shipped default must be blank — a working shared secret in a "
            "public repo is an authentication bypass, not a convenience"
        )


class TestCompiledArtifactsNotTracked:
    def test_no_pycache_in_git(self):
        import subprocess
        tracked = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True
        ).stdout.splitlines()
        bad = [f for f in tracked if "__pycache__" in f or f.endswith(".pyc")]
        assert not bad, (
            f"{len(bad)} compiled artifacts tracked in git (they leak source "
            f"state and bloat the repo): {bad[:3]}"
        )
