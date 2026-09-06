"""v5.22.15 — a NEW credential must not be committable.

``test_v52212_no_secrets_in_source.py`` pins the two literals that actually
leaked. It is exact-match, so it would not have caught a third, different
credential — and in fact did not: this scanner's first run found a live-shaped
``llmp-`` key still published in ``docs/qa-notes.md``, months after the audit
that was supposed to have cleared the repo.

Two halves, and the second matters as much as the first:

  1. The tracked tree is clean.
  2. The detector actually detects. A guard that silently degrades into
     "always clean" — one bad regex edit away — is worse than no guard,
     because it is trusted. Every rule is exercised against a planted
     positive, and the suppression paths are exercised against negatives.
"""

import subprocess

import pytest

from tools.secret_scan import (
    ALLOWLIST_PRAGMA,
    git_tracked_files,
    scan_paths,
    scan_text,
    shannon_entropy,
)

# Planted payloads live here, each excused once, so the assertions below stay
# short enough to read. The pragma excuses THIS file's source line; the string
# handed to scan_text carries no pragma, so detection is still exercised.
_PLANTED_JWT = (
    'k = "eyJhbGciOiJIUzI1NiJ9.'  # pragma: allowlist secret
    'eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij"'
)
_PLANTED_ENTROPY = 'TOKEN = "xQ7fVn2LpR9wZk4TbY6mHc3JsD8gA5eU"'  # pragma: allowlist secret


class TestTreeIsClean:
    def test_no_secrets_in_tracked_files(self):
        findings = scan_paths(git_tracked_files())
        assert findings == [], "possible credentials in tracked source:\n" + "\n".join(
            f"  {f}" for f in findings
        )

    def test_scanner_runs_as_a_cli_and_reports_clean(self):
        """The pre-commit hook and CI both shell out to this — the exit code
        is the contract, so assert on it rather than on the import path."""
        proc = subprocess.run(
            ["python3", "tools/secret_scan.py"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr


class TestDetectorActuallyDetects:
    """Planted positives. If any of these stops firing, the guard is broken."""

    @pytest.mark.parametrize(
        "rule,payload",
        [
            ("aws-access-key-id", 'AWS_ID = "AKIA' + "IOSFODNN7EXAMPLE"[:16] + '"'),
            ("anthropic-api-key", 'k = "sk-ant-' + "a1B2c3D4e5F6g7H8i9J0k1L2" + '"'),
            ("openai-api-key", 'k = "sk-' + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6" + '"'),
            ("llm-proxy-key", 'k = "llmp-' + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ" + '"'),
            ("github-token", 'k = "ghp_' + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7" + '"'),
            ("slack-token", 'k = "xoxb-' + "1234567890-abcdefghij" + '"'),
            ("google-api-key", 'k = "AIza' + "a" * 35 + '"'),
            ("stripe-key", 'k = "sk_live_' + "a1b2c3d4e5f6g7h8i9j0k1" + '"'),
            ("private-key-block", "-----BEGIN RSA PRIVATE KEY-----"),  # pragma: allowlist secret
            ("jwt", _PLANTED_JWT),
        ],
    )
    def test_vendor_rule_fires(self, rule, payload):
        found = scan_text("planted.py", payload)
        assert any(f.rule == rule for f in found), (
            f"rule {rule!r} did not fire on a planted credential: {found}"
        )

    def test_high_entropy_assignment_fires(self):
        found = scan_text("planted.py", _PLANTED_ENTROPY)
        assert any(f.rule == "high-entropy-assignment" for f in found), found

    def test_finding_never_echoes_the_whole_secret(self):
        """A guard that prints the credential into CI logs has moved the leak
        rather than closed it."""
        secret = "AKIA" + "IOSFODNN7EXAMPLE"[:16]
        found = scan_text("planted.py", f'k = "{secret}"')
        assert found
        for f in found:
            assert secret not in f.redacted
            assert secret not in str(f)


class TestSuppressionPaths:
    def test_pragma_suppresses(self):
        line = 'k = "AKIA' + "IOSFODNN7EXAMPLE"[:16] + f'"  # {ALLOWLIST_PRAGMA}'
        assert scan_text("planted.py", line) == []

    @pytest.mark.parametrize(
        "line",
        [
            'api_key = "mock-key"',
            'api_key = "new-token"',
            'password = "changeme"',
            'token = "your-token-here"',
            'secret = os.environ["SECRET"]',
            'api_key = "this-is-not-a-jwt-at-all"',
            'token = "${BRIDGE_TOKEN}"',
        ],
    )
    def test_ordinary_fixtures_do_not_trip_it(self, line):
        """The repo is full of these. A guard that cries wolf gets disabled."""
        assert scan_text("fixture.py", line) == []


class TestEntropyHelper:
    def test_random_beats_handwritten(self):
        assert shannon_entropy("xQ7fVn2LpR9wZk4TbY6mHc3JsD8gA5eU") > 3.9
        assert shannon_entropy("mock-key") < 3.5
        assert shannon_entropy("") == 0.0
