"""v3.7.1 — auto-rotation rule evaluator tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.routing.external_rotation import (
    is_currently_at_capacity,
    evaluate_rules_for_provider,
)


# ── is_currently_at_capacity ───────────────────────────────────────


def _provider(*, auto_skip_until=None, auto_skip_reason=None):
    p = MagicMock()
    p.id = "test-provider"
    p.name = "TestProvider"
    p.auto_skip_until = auto_skip_until
    p.auto_skip_reason = auto_skip_reason
    return p


def test_no_skip_set_returns_false():
    p = _provider(auto_skip_until=None)
    assert is_currently_at_capacity(p) is False


def test_skip_in_future_returns_true():
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    p = _provider(auto_skip_until=future)
    assert is_currently_at_capacity(p) is True


def test_skip_in_past_returns_false():
    """Once the skip window passes, provider becomes available even
    before the rule evaluator clears the field."""
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    p = _provider(auto_skip_until=past)
    assert is_currently_at_capacity(p) is False


def test_naive_datetime_treated_as_utc():
    """SQLite stores naive datetimes. Ensure we don't crash."""
    future_naive = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
    p = _provider(auto_skip_until=future_naive)
    assert is_currently_at_capacity(p) is True


# ── evaluate_rules_for_provider ────────────────────────────────────


def _snapshot(util, resets_in_hours=2):
    s = MagicMock()
    s.id = 1
    s.seven_day_utilization = util
    s.seven_day_resets_at = datetime.now(timezone.utc) + timedelta(hours=resets_in_hours)
    # v5.0.15 — the rotation logic now also reads ``five_hour_*``.
    # Default both to None so these weekly-only tests exercise the
    # weekly branch exclusively (preserves pre-v5.0.15 semantics).
    # Tests for the session bucket live in
    # ``test_v5015_external_rotation_five_hour.py``.
    s.five_hour_utilization = None
    s.five_hour_resets_at = None
    return s


@pytest.mark.asyncio
async def test_rule_sets_skip_when_at_capacity():
    """Gmail-at-100% scenario: rule should set auto_skip_until to the
    snapshot's reset timestamp."""
    snap = _snapshot(util=100.0)
    p = _provider(auto_skip_until=None)
    db = MagicMock()
    out = await evaluate_rules_for_provider(db, p, snapshot=snap)
    assert out["decision"] == "skip_set"
    assert p.auto_skip_until == snap.seven_day_resets_at
    assert "100.0%" in p.auto_skip_reason


@pytest.mark.asyncio
async def test_rule_sets_skip_at_threshold_boundary():
    """95% triggers skip (>= threshold)."""
    snap = _snapshot(util=95.0)
    p = _provider(auto_skip_until=None)
    out = await evaluate_rules_for_provider(MagicMock(), p, snapshot=snap)
    assert out["decision"] == "skip_set"


@pytest.mark.asyncio
async def test_rule_no_change_in_hysteresis_band():
    """94% — below capacity threshold but above clear-below.
    Don't change state."""
    snap = _snapshot(util=94.0)
    prior_skip = datetime.now(timezone.utc) + timedelta(hours=2)
    p = _provider(auto_skip_until=prior_skip)
    out = await evaluate_rules_for_provider(MagicMock(), p, snapshot=snap)
    assert out["decision"] == "no_change"
    assert p.auto_skip_until == prior_skip


@pytest.mark.asyncio
async def test_rule_clears_skip_when_below_clear_threshold():
    """89% — below clear-below (90%). Clear the skip even if still set."""
    snap = _snapshot(util=89.0)
    prior_skip = datetime.now(timezone.utc) + timedelta(hours=2)
    p = _provider(auto_skip_until=prior_skip)
    out = await evaluate_rules_for_provider(MagicMock(), p, snapshot=snap)
    assert out["decision"] == "skip_cleared"
    assert p.auto_skip_until is None
    assert p.auto_skip_reason is None


@pytest.mark.asyncio
async def test_rule_no_change_when_already_clear_and_low():
    """24% (VG-like) with no prior skip. Should be no_change."""
    snap = _snapshot(util=24.0)
    p = _provider(auto_skip_until=None)
    out = await evaluate_rules_for_provider(MagicMock(), p, snapshot=snap)
    assert out["decision"] == "no_change"
    assert p.auto_skip_until is None


@pytest.mark.asyncio
async def test_rule_handles_missing_snapshot():
    p = _provider(auto_skip_until=None)
    db = MagicMock()
    # Make the SELECT return nothing
    rs = MagicMock()
    rs.scalar_one_or_none.return_value = None
    async def execute_mock(*a, **kw): return rs
    db.execute = execute_mock
    out = await evaluate_rules_for_provider(db, p)
    assert out["decision"] == "no_snapshot"


@pytest.mark.asyncio
async def test_rule_handles_null_utilization():
    snap = MagicMock()
    snap.seven_day_utilization = None
    snap.seven_day_resets_at = datetime.now(timezone.utc) + timedelta(hours=2)
    # v5.0.15: rotation also reads five_hour_*; "no data" means BOTH
    # are None. Explicit set preserves the test's "no_utilization"
    # semantic against the new dual-bucket logic.
    snap.five_hour_utilization = None
    snap.five_hour_resets_at = None
    p = _provider(auto_skip_until=None)
    out = await evaluate_rules_for_provider(MagicMock(), p, snapshot=snap)
    assert out["decision"] == "no_utilization"


# ── router integration regression ─────────────────────────────────


def test_router_filters_at_capacity_providers():
    """Source-level check that select_provider applies the auto-skip
    filter from external_rotation."""
    from pathlib import Path
    src = Path("app/routing/router.py").read_text()
    assert "is_currently_at_capacity" in src
    assert "external_rotation" in src
    # Defensive fallback if all are at-capacity
    assert "all_providers_at_capacity" in src


def test_scraper_invokes_rotation_evaluator():
    """After a successful scrape, the evaluator must run on the fresh
    snapshot before commit."""
    from pathlib import Path
    src = Path("app/providers/anthropic_billing.py").read_text()
    assert "evaluate_rules_for_provider" in src
    # Must call db.flush() before evaluator (so snapshot.id is populated)
    # and then a single commit covers both the snapshot insert and the
    # provider's auto_skip_until update.
    assert "db.flush()" in src


def test_settings_have_threshold_fields():
    from app.config import settings
    assert hasattr(settings, "external_rotation_capacity_pct")
    assert hasattr(settings, "external_rotation_hysteresis_pct")
    assert 0 < settings.external_rotation_capacity_pct <= 100
    assert 0 <= settings.external_rotation_hysteresis_pct < settings.external_rotation_capacity_pct


def test_provider_model_has_auto_skip_columns():
    from app.models.db import Provider
    cols = {c.name for c in Provider.__table__.columns}
    assert "auto_skip_until" in cols
    assert "auto_skip_reason" in cols


def test_admin_endpoint_for_manual_evaluate_exists():
    from pathlib import Path
    src = Path("app/api/anthropic_billing.py").read_text()
    assert "_evaluate-rotation-rules" in src
    assert "evaluate_rules_for_all_providers" in src
