"""v5.17.0 (#508 P1-4) — per-account utilization propagation.

Closes P1-4 from the 2026-07-01 backlog. Static-grep pins the
cursor_billing scrape path to (a) pick the least-recently-used enabled
account for its scrape token and (b) write the resulting
``seven_day_utilization`` back to that account's ``utilization_pct`` so
the v5.15.1 ``least_utilized`` picker has real signal to sort on.
"""
from __future__ import annotations
from pathlib import Path


def test_scrape_picks_least_recently_used_account():
    """The scrape MUST prefer an account with the oldest last_used_at so
    over multiple sweeps every account gets scraped in turn."""
    src = Path("app/providers/cursor_billing.py").read_text()
    assert "from app.models.db import ExternalUsageSnapshot, ProviderOAuthAccount" in src
    assert "order_by(ProviderOAuthAccount.last_used_at.asc().nulls_first())" in src
    # The picked account's token is used, not the legacy Provider.api_key.
    assert "scrape_token = _account.access_token" in src


def test_legacy_fallback_preserved_when_no_accounts():
    """Providers with zero accounts must still scrape via
    Provider.api_key — the v5.15.0 backward-compat contract."""
    src = Path("app/providers/cursor_billing.py").read_text()
    assert "scrape_token = provider.api_key" in src


def test_utilization_writeback_to_picked_account():
    """After a successful scrape, ``seven_day_utilization`` MUST be
    written to the picked account's ``utilization_pct``. This is what
    makes the v5.15.1 ``least_utilized`` picker actually able to
    distinguish accounts."""
    src = Path("app/providers/cursor_billing.py").read_text()
    assert "utilization_pct=float(util)" in src
    # Guarded by result.ok (only propagate valid scrape)
    assert "if _account_id_for_writeback is not None and result.ok:" in src


def test_writeback_is_best_effort():
    """Writeback failure MUST NOT fail the scrape — the snapshot is the
    source of truth; the account write is a picker-hint metric."""
    src = Path("app/providers/cursor_billing.py").read_text()
    assert "cursor_billing.per_account_util_writeback_failed" in src
    assert "except Exception as _write_e:" in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 17, 0), (
        f"expected >= 5.17.0, got {major}.{minor}.{patch}"
    )
