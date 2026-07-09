"""v5.7.15 — burst-trigger force-open CB on empty-success spikes.

This closes the 30-min gap between AI-supervisor sweeps that the
2026-06-17 c1conv Gemini incident sat in. The supervisor's LLM-classify
path stays; this is a separate cheap DB sweep that fires every 60s.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ── structural pins ────────────────────────────────────────────────────


def test_module_exists():
    """The new monitoring module is in place."""
    from app.monitoring import empty_success_burst_trigger  # noqa: F401


def test_start_function_exists():
    """``start()`` is the worker entry point — main.py imports + calls it."""
    from app.monitoring.empty_success_burst_trigger import start
    assert callable(start)


def test_wired_into_main():
    """main.py imports and starts the worker. Without this, the worker
    exists but never runs — a silent regression."""
    src = Path("app/main.py").read_text()
    assert "empty_success_burst_trigger" in src
    assert "_esbt.start()" in src


def test_settings_exposed():
    """The 4 burst-trigger settings are on the Settings model."""
    from app.config import settings
    assert hasattr(settings, "empty_success_burst_trigger_enabled")
    assert hasattr(settings, "empty_success_burst_interval_sec")
    assert hasattr(settings, "empty_success_burst_window_sec")
    assert hasattr(settings, "empty_success_burst_threshold")


def test_defaults_match_design():
    """Defaults: enabled=True (this is the operator-escalated fix), 60s
    sweep interval, 300s window, 3 events threshold. Changing any of
    these without updating the changelog is a regression — the
    operator-locked tuning is in v5.7.15 notes."""
    from app.config import settings
    assert settings.empty_success_burst_trigger_enabled is True
    assert settings.empty_success_burst_interval_sec == 60
    assert settings.empty_success_burst_window_sec == 300
    assert settings.empty_success_burst_threshold == 3


def test_version_bumped():
    """v5.7.15 minimum — later patches keep this passing."""
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (5, 7, 15), f"v5.7.15 must be reachable; got {__version__}"


# ── behavioural pins ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_once_no_rows_is_noop():
    """Empty activity_log → 0 detected, 0 opened, no CB action."""
    from app.monitoring import empty_success_burst_trigger as esbt

    fake_force_open_calls = []

    async def fake_force_open(pid):
        fake_force_open_calls.append(pid)

    class FakeResult:
        def fetchall(self):
            return []

    class FakeDB:
        async def execute(self, *a, **kw):
            return FakeResult()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    with patch("app.models.database.AsyncSessionLocal", FakeDB), \
         patch("app.routing.circuit_breaker.force_open", fake_force_open), \
         patch("app.routing.circuit_breaker.get_all_states", lambda: {}):
        result = await esbt._scan_once()

    assert result == {"detected": 0, "opened": 0, "already_open": 0}
    assert fake_force_open_calls == []


@pytest.mark.asyncio
async def test_scan_once_burst_force_opens_closed_provider():
    """A provider over threshold with a currently-closed CB → force_open
    fired, audit row written via log_event."""
    from app.monitoring import empty_success_burst_trigger as esbt

    forced = []
    audited = []

    async def fake_force_open(pid):
        forced.append(pid)

    async def fake_log_event(db, **kw):
        audited.append(kw)

    class FakeResult:
        def fetchall(self):
            return [("provider-A", 7)]

    class FakeDB:
        async def execute(self, *a, **kw):
            return FakeResult()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    with patch("app.models.database.AsyncSessionLocal", FakeDB), \
         patch("app.routing.circuit_breaker.force_open", fake_force_open), \
         patch("app.routing.circuit_breaker.get_all_states",
               lambda: {"provider-A": {"state": "closed", "failures": 7}}), \
         patch("app.monitoring.activity.log_event", fake_log_event):
        result = await esbt._scan_once()

    assert result["detected"] == 1
    assert result["opened"] == 1
    assert result["already_open"] == 0
    assert forced == ["provider-A"]
    # Audit row carries event_type + provider_id + the count
    assert len(audited) == 1
    assert audited[0]["event_type"] == "streaming.burst_force_open"
    assert audited[0]["provider_id"] == "provider-A"
    assert audited[0]["metadata"]["burst_count"] == 7


@pytest.mark.asyncio
async def test_scan_once_skips_already_open_provider():
    """A provider over threshold but with CB already open → no force_open,
    counted as already_open. Prevents log spam during sustained bursts."""
    from app.monitoring import empty_success_burst_trigger as esbt

    forced = []
    audited = []

    async def fake_force_open(pid):
        forced.append(pid)

    async def fake_log_event(db, **kw):
        audited.append(kw)

    class FakeResult:
        def fetchall(self):
            return [("provider-B", 5)]

    class FakeDB:
        async def execute(self, *a, **kw):
            return FakeResult()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    with patch("app.models.database.AsyncSessionLocal", FakeDB), \
         patch("app.routing.circuit_breaker.force_open", fake_force_open), \
         patch("app.routing.circuit_breaker.get_all_states",
               lambda: {"provider-B": {"state": "open", "failures": 12}}), \
         patch("app.monitoring.activity.log_event", fake_log_event):
        result = await esbt._scan_once()

    assert result["detected"] == 1
    assert result["opened"] == 0
    assert result["already_open"] == 1
    assert forced == []
    assert audited == []
