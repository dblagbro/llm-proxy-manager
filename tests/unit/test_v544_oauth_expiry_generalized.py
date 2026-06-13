"""v5.4.4 — generalized OAuth expiry monitor + 15-day threshold + activity_log write.

Operator ask 2026-06-12: "we need 15 day warnings on all expiry issues
like this in the ui". Pre-v5.4.4 the cursor_oauth_expiry_monitor was
cursor-specific, 14-day threshold, and only logged to stderr.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_default_threshold_is_15_days():
    """v5.4.4 bumped 14 → 15 per operator ask."""
    from app.monitoring.cursor_oauth_expiry_monitor import (
        _DEFAULT_WARN_THRESHOLD_DAYS,
    )
    assert _DEFAULT_WARN_THRESHOLD_DAYS == 15


def test_scan_widens_beyond_cursor_oauth():
    """The provider query now includes ALL providers with non-null
    oauth_expires_at, not just provider_type == cursor-oauth."""
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    # Old narrow filter would be ``Provider.provider_type == "cursor-oauth"``
    # alone. v5.4.4 OR-s with ``oauth_expires_at.is_not(None)``.
    assert "Provider.oauth_expires_at.is_not(None)" in src, (
        "v5.4.4 must widen the scan to cover all providers carrying "
        "oauth_expires_at"
    )


def test_scan_writes_activity_log_row_on_warn():
    """Source contract: when warn fires + no warning row in last 24h,
    emit an oauth_expiry_warning row so the UI can render it."""
    src = Path("app/monitoring/cursor_oauth_expiry_monitor.py").read_text()
    assert 'event_type="oauth_expiry_warning"' in src
    assert "ActivityLog(" in src
    # The 24h dedup check must precede the add()
    add_idx = src.find('db.add(ActivityLog(')
    dedup_idx = src.find('"oauth_expiry_warning"')
    assert dedup_idx != -1 and add_idx != -1
    assert dedup_idx < add_idx, "24h dedup check must run before the add()"


def test_ui_badge_added_to_providers_page():
    """The provider card renders an expiry badge when
    oauth_expires_at is set + days_left <= 15."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "p.oauth_expires_at" in src
    assert "v5.4.4" in src  # version comment locking the change
    assert "expires in" in src
    assert "OAuth token expiry" in src


def test_ui_badge_thresholds_match_backend():
    """UI's amber threshold == 15 (matches backend
    OAUTH_EXPIRY_WARN_DAYS_DEFAULT). Red threshold = 3 (tighter, drives
    operator attention for last-week recovery window)."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "daysLeft > 15" in src
    assert "daysLeft <= 3" in src
