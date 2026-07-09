"""v5.14.2 — cluster-sync 403-rate escalation trigger (closes #492).

Operator-confirmed semantics:
- Tag the 50% baseline as known-acceptable noise
- Warn only when 1h rolling rate climbs above ``alert_threshold_pct`` (default 70%)
- AND there's enough sample size (``alert_min_attempts``, default 4)
- AND not in cooldown after a recent fire
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest


# ── (1) Metric module — record + snapshot ─────────────────────────────


def test_metrics_module_imports():
    from app.monitoring.cluster_sync_metrics import (
        record_attempt, snapshot, reset_for_tests,
    )


def test_record_attempt_then_snapshot_counts_correctly():
    from app.monitoring.cluster_sync_metrics import (
        record_attempt, snapshot, reset_for_tests,
    )
    reset_for_tests()
    record_attempt("peer-a", 200)
    record_attempt("peer-a", 200)
    record_attempt("peer-a", 403)
    record_attempt("peer-a", 403)
    record_attempt("peer-a", 500)
    snap = snapshot()
    assert snap["attempts_1h"] == 5
    assert snap["status_200_1h"] == 2
    assert snap["status_403_1h"] == 2
    assert snap["status_other_1h"] == 1
    assert snap["recent_403_pct"] == 40.0  # 2 / 5
    assert snap["last_attempt_status"] == 500
    reset_for_tests()


def test_record_attempt_transport_error_is_separate_bucket():
    """Network errors (status=0) MUST NOT inflate the 403 rate — that's the
    bug v5.14.2 fixes from the original spec."""
    from app.monitoring.cluster_sync_metrics import (
        record_attempt, snapshot, reset_for_tests,
    )
    reset_for_tests()
    for _ in range(5):
        record_attempt("peer-a", 0)  # 5 timeouts
    record_attempt("peer-a", 200)
    snap = snapshot()
    assert snap["status_transport_err_1h"] == 5
    assert snap["status_200_1h"] == 1
    assert snap["status_403_1h"] == 0
    assert snap["recent_403_pct"] == 0.0
    reset_for_tests()


def test_cluster_sync_fresh_flips_on_success_age():
    """Per design: cluster_sync_fresh = True when last 200 was within 600s."""
    from app.monitoring.cluster_sync_metrics import (
        record_attempt, snapshot, reset_for_tests,
    )
    reset_for_tests()
    snap = snapshot()
    assert snap["cluster_sync_fresh"] is False  # no successes yet
    record_attempt("peer-a", 200)
    snap = snapshot()
    assert snap["cluster_sync_fresh"] is True
    reset_for_tests()


# ── (2) Monitor worker — decision logic ───────────────────────────────


def test_under_threshold_does_not_fire():
    """At baseline ~50%, alert threshold 70% → no fire."""
    from app.monitoring import cluster_sync_403_monitor as m
    from app.monitoring.cluster_sync_metrics import (
        record_attempt, reset_for_tests,
    )
    reset_for_tests()
    # 50% 403 rate — exactly the baseline
    for _ in range(5):
        record_attempt("peer-a", 200)
    for _ in range(5):
        record_attempt("peer-a", 403)

    with patch.object(m, "log_event" if False else "_alert_threshold_pct", return_value=70.0):
        with patch.object(m, "_alert_min_attempts", return_value=4):
            with patch("app.monitoring.activity.log_event", new_callable=AsyncMock) as mock_log:
                decision = asyncio.run(m._scan_once())
    assert decision["fired"] is False
    assert "under_threshold" in decision["reason"]
    reset_for_tests()


def test_above_threshold_fires():
    """80% 403 rate exceeds 70% threshold → fire."""
    from app.monitoring import cluster_sync_403_monitor as m
    from app.monitoring.cluster_sync_metrics import (
        record_attempt, reset_for_tests,
    )
    reset_for_tests()
    # Reset module-level cooldown
    m._last_fired_at = 0.0
    for _ in range(2):
        record_attempt("peer-a", 200)
    for _ in range(8):
        record_attempt("peer-a", 403)

    with patch("app.monitoring.activity.log_event", new_callable=AsyncMock) as mock_log:
        decision = asyncio.run(m._scan_once())
    assert decision["fired"] is True
    mock_log.assert_awaited_once()
    args, kwargs = mock_log.await_args
    assert kwargs["event_type"] == "cluster_sync.403_rate_elevated"
    assert kwargs["severity"] == "warning"
    reset_for_tests()


def test_too_few_attempts_does_not_fire():
    """Even 100% 403 rate with attempts < min → no fire."""
    from app.monitoring import cluster_sync_403_monitor as m
    from app.monitoring.cluster_sync_metrics import (
        record_attempt, reset_for_tests,
    )
    reset_for_tests()
    m._last_fired_at = 0.0
    record_attempt("peer-a", 403)
    record_attempt("peer-a", 403)
    record_attempt("peer-a", 403)  # 3 attempts < min 4

    with patch("app.monitoring.activity.log_event", new_callable=AsyncMock) as mock_log:
        decision = asyncio.run(m._scan_once())
    assert decision["fired"] is False
    assert "min_attempts" in decision["reason"]
    mock_log.assert_not_awaited()
    reset_for_tests()


def test_cooldown_suppresses_duplicate_fire():
    """After firing, second scan within cooldown window must be silent."""
    from app.monitoring import cluster_sync_403_monitor as m
    from app.monitoring.cluster_sync_metrics import (
        record_attempt, reset_for_tests,
    )
    reset_for_tests()
    m._last_fired_at = 0.0
    for _ in range(10):
        record_attempt("peer-a", 403)

    with patch("app.monitoring.activity.log_event", new_callable=AsyncMock):
        first = asyncio.run(m._scan_once())
        second = asyncio.run(m._scan_once())
    assert first["fired"] is True
    assert second["fired"] is False
    assert "cooldown" in second["reason"]
    reset_for_tests()


# ── (3) Sender wiring — manager.py calls record_attempt ───────────────


def test_manager_calls_record_attempt_on_success_and_failure():
    """Static-grep — the push_sync function must record_attempt in both the
    success path AND the exception path so transport errors are bucketed
    correctly."""
    src = Path("app/cluster/manager.py").read_text()
    assert "from app.monitoring.cluster_sync_metrics import record_attempt" in src
    assert "record_attempt(peer.id, resp.status_code)" in src
    assert "record_attempt(peer.id, 0)" in src  # transport-err bucket


# ── (4) /health surface ────────────────────────────────────────────────


def test_health_handler_surfaces_clustersync_block():
    src = Path("app/api/cluster.py").read_text()
    assert "_cluster_sync_snapshot" in src
    assert '"clusterSync"' in src


# ── (5) Worker registration in main ────────────────────────────────────


def test_worker_started_in_main():
    src = Path("app/main.py").read_text()
    assert "cluster_sync_403_monitor" in src


# ── (6) Settings ───────────────────────────────────────────────────────


def test_settings_added():
    from app.config import settings
    assert hasattr(settings, "cluster_sync_403_monitor_enabled")
    assert hasattr(settings, "cluster_sync_403_alert_threshold_pct")
    assert hasattr(settings, "cluster_sync_403_alert_min_attempts")
    assert hasattr(settings, "cluster_sync_403_alert_cooldown_sec")
    assert settings.cluster_sync_403_alert_threshold_pct == 70.0
    assert settings.cluster_sync_403_alert_min_attempts == 4


# ── (7) Version ────────────────────────────────────────────────────────


def test_version_bumped():
    """v5.14.2 shipped the escalation trigger; assert at-or-beyond."""
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', src)
    assert m, f"could not parse __version__ from {src!r}"
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 14, 2), (
        f"expected >= 5.14.2, got {major}.{minor}.{patch}"
    )
