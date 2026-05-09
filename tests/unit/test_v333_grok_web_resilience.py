"""v3.3.3 — grok-web resilience pack:

#1 keepalive probe back-off after 429 (rate_limit)
#2 record_request(is_probe=True) is a no-op for provider_metrics
#4 _user_call_timeout reads settings.grok_web_user_timeout_sec

The bridge cool-off (#3) is exercised end-to-end by integration tests
that run the bridge container; this file covers the proxy-side units.
"""
from __future__ import annotations

import time

import pytest


# ── #1 probe back-off ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_keepalive_state():
    """Each test starts with a clean back-off dict so ordering doesn't
    matter (the module dicts persist across tests otherwise)."""
    from app.monitoring import keepalive
    keepalive._probe_backoff_until.clear()
    keepalive._consecutive_rate_limits.clear()
    yield
    keepalive._probe_backoff_until.clear()
    keepalive._consecutive_rate_limits.clear()


def test_is_rate_limit_error_recognises_common_patterns():
    from app.monitoring.keepalive import _is_rate_limit_error
    assert _is_rate_limit_error("HTTP 429: Too Many Requests")
    assert _is_rate_limit_error("rate_limit hit")
    assert _is_rate_limit_error("ThrottledException")
    assert _is_rate_limit_error("ratelimit exceeded")
    # Negatives
    assert not _is_rate_limit_error("")
    assert not _is_rate_limit_error("connection refused")
    assert not _is_rate_limit_error("400 bad request")


def test_backoff_set_on_rate_limit_doubles_with_consecutive_failures(monkeypatch):
    """Each consecutive 429 doubles the next-probe delay until cap."""
    from app.monitoring import keepalive
    # Pin settings for a deterministic check
    monkeypatch.setattr(
        keepalive.settings, "keepalive_probe_interval_sec", 300, raising=False,
    )
    monkeypatch.setattr(
        keepalive.settings, "keepalive_probe_rate_limit_backoff_factor", 2.0,
        raising=False,
    )
    monkeypatch.setattr(
        keepalive.settings, "keepalive_probe_rate_limit_backoff_max_sec", 1800,
        raising=False,
    )

    pid = "p1"
    # First 429: delay = 300 × 2^1 = 600s
    keepalive._record_probe_outcome_for_backoff(pid, "HTTP 429 rate limited")
    state = keepalive.get_backoff_state()[pid]
    assert state["consecutive_rate_limits"] == 1
    assert 590 < state["backoff_remaining_sec"] <= 600

    # Second 429: delay = 300 × 2^2 = 1200s
    keepalive._record_probe_outcome_for_backoff(pid, "429")
    state = keepalive.get_backoff_state()[pid]
    assert state["consecutive_rate_limits"] == 2
    assert 1190 < state["backoff_remaining_sec"] <= 1200

    # Third 429: capped at 1800s (1200 doubled = 2400 → cap)
    # delay = 300 × 2^3 = 2400 → capped at 1800
    keepalive._record_probe_outcome_for_backoff(pid, "rate_limit")
    state = keepalive.get_backoff_state()[pid]
    assert state["consecutive_rate_limits"] == 3
    assert 1790 < state["backoff_remaining_sec"] <= 1800


def test_backoff_resets_on_success():
    """A successful probe (no error_str) clears any in-progress streak."""
    from app.monitoring import keepalive
    pid = "p1"
    keepalive._record_probe_outcome_for_backoff(pid, "429")
    keepalive._record_probe_outcome_for_backoff(pid, "429")
    assert pid in keepalive._consecutive_rate_limits

    # Success
    keepalive._record_probe_outcome_for_backoff(pid, "")
    assert pid not in keepalive._consecutive_rate_limits
    assert pid not in keepalive._probe_backoff_until


def test_backoff_resets_on_non_rate_limit_failure():
    """A network error (non-rate-limit failure) also clears state — we
    don't want unrelated errors to extend the back-off window."""
    from app.monitoring import keepalive
    pid = "p1"
    keepalive._record_probe_outcome_for_backoff(pid, "429")
    assert pid in keepalive._consecutive_rate_limits

    keepalive._record_probe_outcome_for_backoff(pid, "ConnectionRefused")
    assert pid not in keepalive._consecutive_rate_limits


def test_backoff_skip_returns_true_during_window_false_after():
    """_backoff_skip blocks the sweep until the window expires."""
    from app.monitoring import keepalive
    pid = "p1"
    # Manually plant a future window
    keepalive._probe_backoff_until[pid] = time.time() + 60
    assert keepalive._backoff_skip(pid)

    # Past window
    keepalive._probe_backoff_until[pid] = time.time() - 1
    assert not keepalive._backoff_skip(pid)

    # No window at all
    keepalive._probe_backoff_until.pop(pid, None)
    assert not keepalive._backoff_skip(pid)


def test_backoff_disabled_when_factor_le_1(monkeypatch):
    """Factor=1.0 → no back-off (escape hatch for operators)."""
    from app.monitoring import keepalive
    monkeypatch.setattr(
        keepalive.settings, "keepalive_probe_rate_limit_backoff_factor", 1.0,
        raising=False,
    )
    pid = "p1"
    keepalive._record_probe_outcome_for_backoff(pid, "429")
    # State is recorded but no future window
    assert pid not in keepalive._probe_backoff_until


# ── #2 is_probe excludes from provider_metrics ────────────────────────


@pytest.mark.asyncio
async def test_record_request_is_probe_skips_metrics(monkeypatch):
    """is_probe=True must not write a ProviderMetric row, must not
    update ApiKey totals, must not commit."""
    from app.monitoring import metrics

    db_calls = {"execute": 0, "commit": 0, "add": 0}

    class FakeDB:
        async def execute(self, *_a, **_k):
            db_calls["execute"] += 1
            class _R:
                def scalar_one_or_none(self): return None
            return _R()
        async def commit(self):
            db_calls["commit"] += 1
        def add(self, _o):
            db_calls["add"] += 1

    fake = FakeDB()
    await metrics.record_request(
        fake, "p1", True, 100, 50, 1500.0, 0.001,
        api_key_id="k1", ttft_ms=50.0, is_probe=True,
    )
    assert db_calls == {"execute": 0, "commit": 0, "add": 0}, (
        "is_probe=True should be a no-op; saw " + str(db_calls)
    )


@pytest.mark.asyncio
async def test_record_request_real_traffic_still_writes(monkeypatch):
    """is_probe=False (default) goes through the normal upsert path."""
    from app.monitoring import metrics

    seen_writes = []

    class FakeQueryResult:
        def scalar_one_or_none(self):
            return None  # no existing bucket → triggers ProviderMetric()

    class FakeDB:
        async def execute(self, *_a, **_k):
            return FakeQueryResult()
        async def commit(self):
            seen_writes.append("commit")
        def add(self, obj):
            seen_writes.append(("add", type(obj).__name__))

    fake = FakeDB()
    # Stub get_all_states so circuit-breaker import doesn't fail
    monkeypatch.setattr(metrics, "get_all_states", lambda: {})
    await metrics.record_request(
        fake, "p1", True, 100, 50, 1500.0, 0.001,
        api_key_id="k1", ttft_ms=50.0, is_probe=False,
    )
    # Should have at minimum: 1 add (new metric) + 1 execute (api_key
    # update) + 1 commit
    assert any(w == "commit" for w in seen_writes), (
        "real traffic should commit; saw " + str(seen_writes)
    )
    assert any(isinstance(w, tuple) and w[0] == "add" for w in seen_writes), (
        "real traffic should add a ProviderMetric; saw " + str(seen_writes)
    )


# ── #4 user-call timeout setting ──────────────────────────────────────


def test_user_call_timeout_reads_setting(monkeypatch):
    from app.api import _grok_web_dispatch as gwd
    monkeypatch.setattr(gwd.settings, "grok_web_user_timeout_sec", 8, raising=False)
    assert gwd._user_call_timeout() == 8.0

    monkeypatch.setattr(gwd.settings, "grok_web_user_timeout_sec", 30, raising=False)
    assert gwd._user_call_timeout() == 30.0


def test_user_call_timeout_handles_missing_setting(monkeypatch):
    """If the setting is somehow None / absent, fall back to 30s."""
    from app.api import _grok_web_dispatch as gwd
    monkeypatch.setattr(gwd.settings, "grok_web_user_timeout_sec", None, raising=False)
    assert gwd._user_call_timeout() == 30.0
