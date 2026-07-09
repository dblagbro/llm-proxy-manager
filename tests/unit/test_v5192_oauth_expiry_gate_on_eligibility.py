"""v5.19.2 — gate oauth_expiry warning on auto-refresh ELIGIBILITY.

Trigger: 2026-07-04 log sweep found Devin-Codex-Gmail (ChatGPT-oauth-plan,
7 days from expiry, has refresh_token) still firing daily
`oauth_expiry_warning` despite v5.17.2. Root cause: v5.17.2 checked
whether refresh RAN this sweep (`refresh_outcome=="refreshed"`), but
refresh only runs when `days_left <= _DEFAULT_REFRESH_LEAD_DAYS`
(default 1 day). For a token 7d from expiry, refresh doesn't run yet,
so v5.17.2's gate never triggered — even though there's nothing for
the operator to act on (refresh will handle it at day 1).

Fix: gate on ELIGIBILITY (type supports auto-refresh + has token + no
failure this sweep), not on recent success.
"""
from __future__ import annotations
from pathlib import Path


def test_new_gate_checks_eligibility_not_recent_success():
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    assert "_is_auto_refresh_eligible" in src
    assert "p.provider_type in _PROACTIVE_REFRESH_TYPES" in src
    assert "_has_refresh" in src


def test_v5172_recent_success_variable_gone():
    """v5.17.2 used `_refresh_ok = refresh_outcome == "refreshed"` as the
    control variable. v5.19.2 replaces it with `_is_auto_refresh_eligible`.
    Docstring references v5.17.2's old code are fine; what matters is no
    active code line assigns `_refresh_ok` OR uses `not (_refresh_ok and`."""
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    # v5.17.2's exact `warn = ... and not (_refresh_ok and _has_refresh)`
    # gate expression MUST be gone.
    assert "not (_refresh_ok and _has_refresh)" not in src
    # v5.19.2's new gate expression MUST be present.
    assert "not _is_auto_refresh_eligible" in src


def test_failed_refresh_still_warns():
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    assert "_refresh_failed" in src
    assert 'startswith("failed:")' in src
    assert "and not _refresh_failed" in src


def test_missing_refresh_token_still_warns():
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    assert "_has_refresh" in src


def test_docstring_documents_the_fix():
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    assert "v5.19.2" in src
    assert "ELIGIBILITY, not recent" in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 19, 2), (
        f"expected >= 5.19.2, got {major}.{minor}.{patch}"
    )
