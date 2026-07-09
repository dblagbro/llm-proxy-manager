"""v5.7.21 — proactive OAuth refresh in the existing expiry monitor.

Operator-flagged 2026-06-18: two claude-oauth providers on the clone
cluster showed "expires in 0d" in the UI. Refresh tokens were present
but unused — the existing lazy "refresh on 401" path needs traffic to
fire, and these providers are low-priority (7/8) so they get no
traffic. Counter ticks down even though refresh would have worked.

This ship adds a proactive refresh step to the EXISTING
cursor_oauth_expiry_monitor sweep (already widened in v5.4.4 to scan
all OAuth provider types). Any claude-oauth or ChatGPT-oauth-plan
provider whose token is within REFRESH_LEAD_DAYS of expiry, has a
refresh token, AND is enabled gets refreshed in place.

Also drops the sweep interval from 6h → 1h and the initial delay
from 2h → 5min so the very next sweep after deploy fixes the
immediate badge.
"""
from __future__ import annotations

from pathlib import Path


# ── structural pins ────────────────────────────────────────────────────


def test_refresh_lead_threshold_set():
    """The new threshold constant exists with a 24h default."""
    from app.monitoring import cursor_oauth_expiry_monitor as m
    assert getattr(m, "_DEFAULT_REFRESH_LEAD_DAYS", None) == 1.0


def test_proactive_refresh_types_includes_claude():
    """claude-oauth is in the proactive-refresh list — that's the
    type the 2026-06-18 ship targets."""
    from app.monitoring import cursor_oauth_expiry_monitor as m
    types = getattr(m, "_PROACTIVE_REFRESH_TYPES", ())
    assert "claude-oauth" in types
    # codex too, since it has the same refresh_and_persist helper
    assert "ChatGPT-oauth-plan" in types


def test_proactive_refresh_types_excludes_cursor():
    """cursor-oauth is intentionally NOT in the list — its refresh
    flow is gated on empirical confirmation per the noVNC backlog."""
    from app.monitoring import cursor_oauth_expiry_monitor as m
    types = getattr(m, "_PROACTIVE_REFRESH_TYPES", ())
    assert "cursor-oauth" not in types, (
        "v5.7.21: cursor-oauth proactive refresh stays disabled until "
        "the v4.4.37 refresh_token probe confirms the flow works."
    )


def test_sweep_interval_dropped_to_1h():
    """6h → 1h. A failed refresh now retries within 1h instead of
    waiting 6h, which would miss the 1-day refresh window."""
    from app.monitoring import cursor_oauth_expiry_monitor as m
    assert m._SWEEP_INTERVAL_SEC == 3600, (
        f"v5.7.21: sweep interval should be 1h; got {m._SWEEP_INTERVAL_SEC}s"
    )


def test_initial_delay_dropped_to_5min():
    """2h → 5min. The legacy "don't race the cursor billing scraper"
    concern is moot now that the refresh path is in this worker."""
    from app.monitoring import cursor_oauth_expiry_monitor as m
    assert m._INITIAL_DELAY_SEC == 300, (
        f"v5.7.21: initial delay should be 5min; got {m._INITIAL_DELAY_SEC}s"
    )


def test_module_source_has_refresh_path():
    """Source-grep pin: the worker module contains the refresh-and-
    persist call site so a refactor can't silently strip it."""
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    assert "refresh_and_persist" in src
    assert "_DEFAULT_REFRESH_LEAD_DAYS" in src
    assert "proactive_refresh" in src


def test_snapshot_carries_refresh_outcome():
    """The per-provider snapshot includes ``refresh_attempted`` and
    ``refresh_outcome`` so the admin endpoint can show whether each
    provider got refreshed this sweep."""
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    assert '"refresh_attempted": refresh_attempted' in src
    assert '"refresh_outcome": refresh_outcome' in src


def test_refresh_failure_marks_auth_failed():
    """Source-grep pin: on refresh failure we call record_auth_failure
    so the UI surfaces "needs re-auth" BEFORE real traffic hits the
    dead token and 401s twice."""
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    assert "from app.routing.circuit_breaker import record_auth_failure" in src
    assert "record_auth_failure(p.id" in src


def test_version_bumped():
    """v5.7.21 minimum — later patches keep this passing."""
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (5, 7, 21), f"v5.7.21 must be reachable; got {__version__}"
