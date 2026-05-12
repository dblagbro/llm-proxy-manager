"""v3.7.24 (#258) — Anthropic billing scrape freshness guard.

Verifies the worker skips a scrape when a fresh snapshot for the
provider already exists. Closes the operator complaint that container
restarts + per-node-independent scrapes were producing "every few
minutes" snapshot rows during deploy bursts.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Source-level expectations ────────────────────────────────────


def test_worker_imports_random_for_jitter():
    src = Path("app/monitoring/anthropic_billing_worker.py").read_text()
    assert "import random" in src
    assert "_STARTUP_JITTER_MAX_SEC" in src


def test_worker_has_freshness_floor_helper():
    src = Path("app/monitoring/anthropic_billing_worker.py").read_text()
    assert "_freshness_floor_sec" in src
    # Default is interval/2 with a hard minimum of 60s
    assert "interval_sec // 2" in src


def test_worker_has_latest_snapshot_age_helper():
    src = Path("app/monitoring/anthropic_billing_worker.py").read_text()
    assert "_latest_snapshot_age_sec" in src
    # Uses MAX subquery, not the legacy ORDER BY + LIMIT pattern
    assert "func.max(ExternalUsageSnapshot.captured_at)" in src


def test_scrape_all_once_checks_freshness_before_scraping():
    src = Path("app/monitoring/anthropic_billing_worker.py").read_text()
    idx = src.index("async def _scrape_all_once")
    body = src[idx:idx + 3000]
    assert "_latest_snapshot_age_sec" in body
    assert "_freshness_floor_sec" in body
    # Skip path must continue without scraping
    assert "continue" in body
    # Must surface a skipped count + debug-level log
    assert "skipped" in body
    assert "anthropic_billing.skip_fresh" in body


def test_scrape_loop_applies_startup_jitter():
    src = Path("app/monitoring/anthropic_billing_worker.py").read_text()
    idx = src.index("async def _scrape_loop")
    body = src[idx:idx + 1500]
    assert "random.uniform" in body
    assert "WARMUP_DELAY_SEC + jitter" in body


def test_config_exposes_min_scrape_gap_override():
    from app.config import settings
    assert hasattr(settings, "anthropic_billing_min_scrape_gap_sec")
    # Default 0 means "use the interval/2 heuristic"
    assert settings.anthropic_billing_min_scrape_gap_sec == 0


# ── Runtime behavior ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_freshness_helper_returns_none_when_no_snapshots():
    from app.monitoring.anthropic_billing_worker import _latest_snapshot_age_sec
    fake_result = MagicMock()
    fake_result.scalar = MagicMock(return_value=None)
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=fake_result)
    age = await _latest_snapshot_age_sec(fake_db, "provider-x")
    assert age is None


@pytest.mark.asyncio
async def test_freshness_helper_returns_positive_age_for_recent_row():
    from app.monitoring.anthropic_billing_worker import _latest_snapshot_age_sec
    five_min_ago = datetime.utcnow() - timedelta(minutes=5)
    fake_result = MagicMock()
    fake_result.scalar = MagicMock(return_value=five_min_ago)
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=fake_result)
    age = await _latest_snapshot_age_sec(fake_db, "provider-x")
    assert age is not None
    # 5 min ago → age between 290 and 320 seconds (tolerate test runtime jitter)
    assert 290 < age < 320


@pytest.mark.asyncio
async def test_freshness_helper_parses_string_timestamps():
    """SQLite returns DATETIME columns as strings via aiosqlite. The
    helper must coerce them to datetime before subtracting."""
    from app.monitoring.anthropic_billing_worker import _latest_snapshot_age_sec
    ten_min_ago = datetime.utcnow() - timedelta(minutes=10)
    fake_result = MagicMock()
    fake_result.scalar = MagicMock(return_value=ten_min_ago.strftime("%Y-%m-%d %H:%M:%S"))
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=fake_result)
    age = await _latest_snapshot_age_sec(fake_db, "provider-x")
    assert age is not None
    assert 590 < age < 620


@pytest.mark.asyncio
async def test_freshness_helper_clamps_future_timestamps():
    """If a peer has a clock slightly ahead of us, the captured_at can
    read as future. Clamp to 0 (treat as fresh) so we still skip."""
    from app.monitoring.anthropic_billing_worker import _latest_snapshot_age_sec
    future = datetime.utcnow() + timedelta(seconds=30)
    fake_result = MagicMock()
    fake_result.scalar = MagicMock(return_value=future)
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=fake_result)
    age = await _latest_snapshot_age_sec(fake_db, "provider-x")
    assert age == 0.0


def test_freshness_floor_default_is_half_interval():
    from app.monitoring.anthropic_billing_worker import _freshness_floor_sec
    # 4h interval → 2h floor
    assert _freshness_floor_sec(14400) == 7200
    # Override-zero behavior: returns interval/2 when override is 0
    # 1h interval → 30min floor
    assert _freshness_floor_sec(3600) == 1800


def test_freshness_floor_has_minimum_60s():
    from app.monitoring.anthropic_billing_worker import _freshness_floor_sec
    # Pathological tiny intervals still get a 60s floor so the guard
    # is meaningful (a 10s interval would otherwise produce a 5s floor
    # which doesn't actually deduplicate anything in practice).
    assert _freshness_floor_sec(30) == 60


def test_freshness_floor_respects_operator_override():
    from app.monitoring.anthropic_billing_worker import _freshness_floor_sec
    with patch("app.monitoring.anthropic_billing_worker._interval_sec", return_value=14400):
        from app.config import settings
        with patch.object(settings, "anthropic_billing_min_scrape_gap_sec", 3600):
            # Operator pinned to 1h
            assert _freshness_floor_sec(14400) == 3600


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 24)
