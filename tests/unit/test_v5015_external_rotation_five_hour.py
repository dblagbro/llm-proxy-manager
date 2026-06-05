"""v5.0.15 — external rotation also respects ``five_hour_utilization``.

Pre-v5.0.15 the rotation logic at
``app/routing/external_rotation.evaluate_rules_for_provider`` only
checked ``seven_day_utilization``. The Anthropic billing snapshot
also captures ``five_hour_utilization`` (Anthropic's session-window
counter, resets every 5h). Operators reproducibly saw the case where
a provider hit 100% session / 13% weekly and the router kept picking
it because the weekly bucket looked healthy.

The fix:
  - Session bucket: hard 100% cap, no hysteresis (it's an upstream
    lockout, not a tunable policy).
  - Weekly bucket: existing soft threshold + hysteresis.
  - Skip if EITHER exhausts.
  - ``auto_skip_until`` = LATER of the two reset times (so we don't
    release prematurely while one bucket is still capped).
  - Clear only when BOTH are confirmed healthy.

These tests exercise the new branches against an in-memory provider
ORM instance (no DB needed — pass ``snapshot`` directly).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.routing.external_rotation import evaluate_rules_for_provider


def _mk_provider(*, auto_skip_until=None, auto_skip_reason=None):
    return SimpleNamespace(
        id="p-test", name="Test",
        auto_skip_until=auto_skip_until,
        auto_skip_reason=auto_skip_reason,
    )


def _mk_snap(
    *,
    seven_day_utilization=None,
    seven_day_resets_at=None,
    five_hour_utilization=None,
    five_hour_resets_at=None,
):
    return SimpleNamespace(
        seven_day_utilization=seven_day_utilization,
        seven_day_resets_at=seven_day_resets_at,
        five_hour_utilization=five_hour_utilization,
        five_hour_resets_at=five_hour_resets_at,
    )


# ── The v5.0.15 incident ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_100pct_skips_until_session_reset_even_when_weekly_healthy():
    """The Devin-Anthropic-Max-VG case on 2026-06-04 — five_hour=100%,
    seven_day=13%. Pre-v5.0.15 this provider stayed in rotation."""
    session_reset = datetime(2026, 6, 4, 23, 40)
    weekly_reset = datetime(2026, 6, 6, 21, 0)
    p = _mk_provider()
    snap = _mk_snap(
        seven_day_utilization=13.0, seven_day_resets_at=weekly_reset,
        five_hour_utilization=100.0, five_hour_resets_at=session_reset,
    )
    out = await evaluate_rules_for_provider(None, p, snapshot=snap)
    assert out["decision"] == "skip_set"
    assert p.auto_skip_until == session_reset
    assert "session utilization" in (p.auto_skip_reason or "")
    assert out["five_hour_utilization"] == 100.0
    assert out["seven_day_utilization"] == 13.0


@pytest.mark.asyncio
async def test_both_buckets_exhausted_skip_until_later_reset():
    """Both buckets at-cap. skip_until should be the LATER reset so
    we don't unskip while one bucket still says we're locked out."""
    session_reset = datetime(2026, 6, 4, 23, 40)
    weekly_reset = datetime(2026, 6, 6, 21, 0)   # later
    p = _mk_provider()
    snap = _mk_snap(
        seven_day_utilization=99.0, seven_day_resets_at=weekly_reset,
        five_hour_utilization=100.0, five_hour_resets_at=session_reset,
    )
    out = await evaluate_rules_for_provider(None, p, snapshot=snap)
    assert out["decision"] == "skip_set"
    assert p.auto_skip_until == weekly_reset, (
        "Both buckets exhausted; auto_skip_until should be the LATER "
        "reset (weekly) — releasing on the earlier (session) reset "
        "while weekly is still capped would route into a locked provider."
    )
    # Reason should mention BOTH buckets
    assert "session utilization" in p.auto_skip_reason
    assert "weekly utilization" in p.auto_skip_reason


@pytest.mark.asyncio
async def test_session_clears_only_when_both_buckets_ok():
    """A provider currently skipped should NOT clear if session is
    still at 100% (even if weekly is fully recovered)."""
    p = _mk_provider(
        auto_skip_until=datetime(2026, 6, 4, 23, 40),
        auto_skip_reason="session utilization 100.0% >= 100%",
    )
    snap = _mk_snap(
        seven_day_utilization=5.0, seven_day_resets_at=None,
        five_hour_utilization=100.0, five_hour_resets_at=datetime(2026, 6, 4, 23, 40),
    )
    out = await evaluate_rules_for_provider(None, p, snapshot=snap)
    # Session is still 100% so we re-set the skip (skip_set) rather
    # than clear it. The point: never skip_cleared while a bucket
    # remains capped.
    assert out["decision"] == "skip_set"
    assert p.auto_skip_until is not None


@pytest.mark.asyncio
async def test_clear_when_both_buckets_recovered():
    """Weekly under hysteresis floor AND session below 100% → clear."""
    p = _mk_provider(
        auto_skip_until=datetime(2026, 6, 4, 0, 0),
        auto_skip_reason="weekly utilization 95% >= 90% threshold",
    )
    snap = _mk_snap(
        seven_day_utilization=10.0, seven_day_resets_at=None,
        five_hour_utilization=20.0, five_hour_resets_at=None,
    )
    out = await evaluate_rules_for_provider(None, p, snapshot=snap)
    assert out["decision"] == "skip_cleared"
    assert p.auto_skip_until is None
    assert p.auto_skip_reason is None


# ── Backward compatibility ───────────────────────────────────────


@pytest.mark.asyncio
async def test_weekly_only_snapshot_still_works():
    """Pre-Anthropic snapshots (cursor-oauth's billing scraper, etc.)
    don't populate five_hour_utilization. The pre-v5.0.15 weekly-only
    behavior must keep working when session data is absent."""
    weekly_reset = datetime(2026, 6, 6, 21, 0)
    p = _mk_provider()
    snap = _mk_snap(
        seven_day_utilization=98.0, seven_day_resets_at=weekly_reset,
        # five_hour_* both None
    )
    out = await evaluate_rules_for_provider(None, p, snapshot=snap)
    assert out["decision"] == "skip_set"
    assert p.auto_skip_until == weekly_reset


@pytest.mark.asyncio
async def test_session_only_snapshot_still_works():
    """If for any reason a snapshot has only session data (not weekly),
    the session bucket should still drive the decision."""
    session_reset = datetime(2026, 6, 4, 23, 40)
    p = _mk_provider()
    snap = _mk_snap(
        five_hour_utilization=100.0, five_hour_resets_at=session_reset,
        # seven_day_* both None
    )
    out = await evaluate_rules_for_provider(None, p, snapshot=snap)
    assert out["decision"] == "skip_set"
    assert p.auto_skip_until == session_reset


@pytest.mark.asyncio
async def test_no_utilization_at_all_returns_no_decision():
    """Both buckets None → no_utilization (regression: don't crash)."""
    p = _mk_provider()
    snap = _mk_snap()    # both buckets None
    out = await evaluate_rules_for_provider(None, p, snapshot=snap)
    assert out["decision"] == "no_utilization"


@pytest.mark.asyncio
async def test_both_buckets_healthy_no_change():
    """Below threshold + below 100% session → no_change (regression)."""
    p = _mk_provider()
    snap = _mk_snap(
        seven_day_utilization=50.0,
        five_hour_utilization=30.0,
    )
    out = await evaluate_rules_for_provider(None, p, snapshot=snap)
    assert out["decision"] == "no_change"
    assert p.auto_skip_until is None
