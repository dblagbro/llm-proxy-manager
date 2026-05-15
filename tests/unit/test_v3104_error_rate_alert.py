"""v3.10.4 — aggregate error-rate alert.

v3.10.1 made operator-actionable failures log as ``severity=error``;
v3.10.4 turns that into an alert. The observability sampler checks the
rolling error rate every ~5 min and, when ``err >= min_count`` AND the
rate ``>= threshold_pct``, fires ``alert_high_error_rate``. Without this
a sustained spike runs unnoticed — the v3.10.0 translation bug went
~3 weeks unalerted.
"""
from __future__ import annotations

from pathlib import Path

from app.monitoring.observability_sampler import _should_alert_error_rate


# ── decision logic ─────────────────────────────────────────────────


def test_no_traffic_never_alerts():
    assert _should_alert_error_rate(0, 0, 10, 10.0) is False


def test_below_min_count_does_not_alert():
    """A handful of errors in a near-idle window must not page — even
    at a high rate. min_count is the low-traffic noise floor."""
    assert _should_alert_error_rate(5, 8, 10, 10.0) is False  # 62% but err<10


def test_below_threshold_does_not_alert():
    """A small fraction of a large volume is not an incident."""
    assert _should_alert_error_rate(15, 10_000, 10, 10.0) is False  # 0.15%


def test_sustained_spike_alerts():
    assert _should_alert_error_rate(50, 100, 10, 10.0) is True  # 50%


def test_translation_bug_scenario_would_have_alerted():
    """The v3.10.0 translation bug: ~69% of requests failing. The alert
    that didn't exist then would fire now."""
    assert _should_alert_error_rate(690, 1000, 10, 10.0) is True


def test_exactly_at_thresholds_alerts():
    """Boundary: exactly min_count errors at exactly threshold_pct."""
    assert _should_alert_error_rate(10, 100, 10, 10.0) is True


def test_threshold_is_configurable():
    # Same traffic, stricter threshold → no alert; looser → alert.
    assert _should_alert_error_rate(20, 100, 10, 50.0) is False  # 20% < 50%
    assert _should_alert_error_rate(20, 100, 10, 5.0) is True    # 20% >= 5%


# ── config ─────────────────────────────────────────────────────────


def test_error_rate_alert_settings_exist():
    from app.config import settings
    assert settings.error_rate_alert_enabled is True
    assert settings.error_rate_alert_window_min == 15
    assert settings.error_rate_alert_threshold_pct == 10.0
    assert settings.error_rate_alert_min_count == 10


# ── notification helper ────────────────────────────────────────────


def test_alert_high_error_rate_exists():
    from app.monitoring import notifications
    assert hasattr(notifications, "alert_high_error_rate")


def test_alert_high_error_rate_is_error_severity_and_throttled():
    src = Path("app/monitoring/notifications.py").read_text()
    idx = src.index("async def alert_high_error_rate(")
    fn = src[idx:idx + 900]
    assert '"error"' in fn  # operator-actionable severity
    assert 'throttle_key="high_error_rate"' in fn  # one mail per incident


# ── sampler wiring ─────────────────────────────────────────────────


def test_sampler_runs_error_rate_check_on_a_cadence():
    """The check rides the existing 30s sampler loop but only fires
    every Nth tick — it must not run a DB scan every 30s."""
    src = Path("app/monitoring/observability_sampler.py").read_text()
    assert "_sample_error_rate" in src
    assert "_ERROR_RATE_CHECK_EVERY" in src
    assert "_tick % _ERROR_RATE_CHECK_EVERY" in src
    # probes are excluded from the rate
    assert '"[probe]" in msg' in src
