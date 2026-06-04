"""v5.0.3 — OpenCode CLI UA compatibility check (P2.1).

Pre-cutover empirical validation: OpenCode (the Coordinator Code profile
candidate) MUST NOT accidentally match any banned-client-product
pattern. Otherwise it would 451 the moment policy turns on.

UA capture procedure (run 2026-06-04 against opencode-ai v1.15.13 in a
throwaway dir):

    1. ``npm install opencode-ai`` in /tmp
    2. tiny Python HTTP server on 127.0.0.1:18901 logging User-Agent
    3. ``opencode providers login http://127.0.0.1:18901/v1`` (which
       short-circuits on a malformed response BUT not before its first
       outbound HTTP call reaches the capture server)
    4. captured UA: ``opencode/1.15.13``

The format is ``opencode/<semver>`` — a clean, narrow product name with
no Anthropic/OpenAI/SDK lineage in it. None of the banned-product
patterns in ``app.compliance.company_map.KNOWN_COMPANIES`` match it.

This test pins:

- The exact captured UA passes detection (None).
- A range of plausible future versions still pass (so a v1.15.14 or v2.x
  bump doesn't surprise the hub team).
- The narrow-anchored patterns in our taxonomy do NOT regress to
  matching ``opencode/...`` via a near-miss (e.g. a future contributor
  shortening the ``claude-code/`` pattern to ``code/`` would catch
  OpenCode + many other tools).
"""
import pytest

from app.compliance import detect_client_company
from app.compliance.company_map import KNOWN_COMPANIES


# ── Empirically captured ──────────────────────────────────────────────


CAPTURED_OPENCODE_UA = "opencode/1.15.13"
"""Pinned from a live HTTP capture against opencode-ai 1.15.13
(2026-06-04). If a future OpenCode major rev changes its UA format,
update this constant and re-run the capture procedure described in
the module docstring."""


def test_captured_opencode_ua_does_not_match_any_banned_pattern():
    """The actual UA OpenCode 1.15.13 sends MUST pass our detector
    cleanly — otherwise the hub team's cutover 451s."""
    assert detect_client_company(CAPTURED_OPENCODE_UA) is None


# ── Forward-compat ──────────────────────────────────────────────────


@pytest.mark.parametrize("ua", [
    "opencode/1.15.13",
    "opencode/1.15.14",
    "opencode/1.16.0",
    "opencode/2.0.0",
    "opencode/2.0.0-rc.1",
    "opencode/2.0.0+build.123",
    # Bundled-Stainless suffix variant some AI-SDK consumers carry — paranoid
    "opencode/1.15.13 (Stainless)",
])
def test_opencode_version_variants_pass(ua):
    """A range of plausible future OpenCode versions must keep passing.
    Catches the case where a future contributor adds a pattern that
    matches ``opencode/`` itself."""
    assert detect_client_company(ua) is None, (
        f"UA {ua!r} would now be banned — check the taxonomy for a new pattern"
    )


# ── Narrow-pattern discipline cover ─────────────────────────────────


def test_no_known_company_pattern_starts_with_opencode():
    """If any company gains a prefix/contains pattern that matches
    ``opencode/`` literally, this test fires. (Custom companies are
    NOT enumerated here — those are operator-controlled.)"""
    for company_id, info in KNOWN_COMPANIES.items():
        for rule in info.get("ua_patterns", []):
            value = rule.get("value", "")
            ptype = rule.get("type")
            if ptype == "prefix":
                assert not "opencode/".startswith(value.lower()), (
                    f"Pattern '{value}' in company '{company_id}' would "
                    f"false-positive on opencode/..."
                )
            elif ptype == "contains":
                assert value.lower() not in "opencode/1.15.13", (
                    f"Pattern '{value}' in company '{company_id}' would "
                    f"false-positive on opencode/..."
                )


def test_negative_control_claude_cli_still_blocks():
    """Sanity — the patterns that SHOULD fire still do. If this test
    breaks, the same refactor that broke OpenCode probably also broke
    the actual Anthropic-product detection."""
    result = detect_client_company("claude-cli/2.1.88")
    assert result is not None
    assert result[0] == "anthropic"
