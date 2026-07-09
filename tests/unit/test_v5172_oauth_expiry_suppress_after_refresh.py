"""v5.17.2 — suppress oauth_expiry warning when proactive refresh just succeeded.

Trigger: log sweep 2026-07-02 found `Devin-Anthropic-Max-VG` and
`Devin-Anthropic-Max-Gmail` firing oauth_expiry_warning every sweep
(threshold 15d, access token life 0.3d after refresh). Refresh
succeeded — nothing for the operator to act on. Pure noise.

Fix: after refresh_outcome == "refreshed" AND provider has a
refresh_token, suppress the low-days-left warning. Failed-refresh path
still warns; missing-refresh-token path still warns.
"""
from __future__ import annotations
from pathlib import Path


def test_expiry_monitor_suppresses_warn_after_successful_refresh():
    """v5.17.2's gate suppressed the warning after a successful refresh.
    v5.19.2 broadens the gate to any provider ELIGIBLE for auto-refresh
    (type + refresh_token + no failure this sweep) — which is a
    superset of "just refreshed successfully." The v5.17.2 intent is
    preserved (successful refresh = suppress) but the gate now also
    covers the "will-refresh-eventually" case for tokens further out
    than the refresh-lead window."""
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    # v5.19.2 gate present
    assert "_is_auto_refresh_eligible" in src
    # Failed refresh still warns — the eligibility check excludes failures
    assert "_refresh_failed" in src


def test_failed_refresh_still_warns():
    """v5.17.2's gate suppressed only refresh_outcome == 'refreshed'.
    v5.19.2 replaces this with an eligibility check that specifically
    excludes failed refresh outcomes via `_refresh_failed`. So
    failed:* still triggers the warning as before."""
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    # Failed refresh is detected via startswith("failed:")
    assert 'startswith("failed:")' in src
    # And included in eligibility check (must NOT be failed to be eligible)
    assert "and not _refresh_failed" in src


def test_missing_refresh_token_still_warns():
    """Providers without a refresh_token get the warning as always —
    they need operator action to re-auth. The gate specifically checks
    _has_refresh."""
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    assert '_has_refresh = bool(getattr(p, "oauth_refresh_token", None))' in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 17, 2), (
        f"expected >= 5.17.2, got {major}.{minor}.{patch}"
    )
