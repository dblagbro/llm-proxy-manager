"""Unit tests for Codex subscription rate-limit tracking (v3.0.16)."""
import time

from app.providers.codex_ratelimit import (
    RateLimitState,
    update_from_headers,
    get_state,
    detect_rate_limit_failure,
    _STATES,
)


def setup_function(_fn):
    _STATES.clear()


def _live_headers():
    """Realistic header bundle from a Plus-tier Codex response."""
    return {
        "x-codex-active-limit": "premium",
        "x-codex-plan-type": "plus",
        "x-codex-primary-used-percent": "2",
        "x-codex-primary-window-minutes": "300",
        "x-codex-primary-reset-after-seconds": "14180",
        "x-codex-secondary-used-percent": "3",
        "x-codex-secondary-window-minutes": "10080",
        "x-codex-secondary-reset-after-seconds": "510106",
    }


class TestUpdateFromHeaders:
    def test_full_header_set_parsed(self):
        before = time.time()
        state = update_from_headers("prov-1", _live_headers())
        assert state.plan_type == "plus"
        assert state.active_limit == "premium"
        assert state.primary_used_percent == 2.0
        assert state.primary_window_minutes == 300
        assert state.secondary_used_percent == 3.0
        assert state.secondary_window_minutes == 10080
        # reset_at is computed from reset-after-seconds + now
        assert state.primary_reset_at > before
        assert state.primary_reset_at < before + 14181
        assert state.last_observed_at >= before

    def test_reset_at_takes_precedence_over_reset_after(self):
        h = _live_headers()
        h["x-codex-primary-reset-at"] = "1777591525"
        state = update_from_headers("prov-2", h)
        assert state.primary_reset_at == 1777591525.0

    def test_partial_headers_keep_existing_state(self):
        first = update_from_headers("prov-3", _live_headers())
        # Second response only carries the primary fields — secondary state
        # from the first response should be preserved (sticky).
        h2 = {
            "x-codex-primary-used-percent": "5",
            "x-codex-primary-reset-after-seconds": "13000",
        }
        second = update_from_headers("prov-3", h2)
        assert second.primary_used_percent == 5.0
        # secondary survived from first
        assert second.secondary_used_percent == first.secondary_used_percent
        assert second.secondary_window_minutes == first.secondary_window_minutes

    def test_get_state_after_update(self):
        update_from_headers("prov-4", _live_headers())
        s = get_state("prov-4")
        assert s is not None
        assert s.plan_type == "plus"

    def test_get_state_empty_returns_none(self):
        assert get_state("never-observed") is None


class TestDetectRateLimitFailure:
    def test_429_returns_primary_holddown(self):
        h = _live_headers()
        holddown = detect_rate_limit_failure(429, "Rate limit exceeded", h)
        assert holddown == 14180.0

    def test_403_with_limit_message_returns_secondary_when_primary_absent(self):
        h = {"x-codex-secondary-reset-after-seconds": "60000"}
        holddown = detect_rate_limit_failure(403, "Plan quota exceeded", h)
        assert holddown == 60000.0

    def test_403_without_limit_message_returns_none(self):
        holddown = detect_rate_limit_failure(403, "Forbidden — invalid scope", {})
        assert holddown is None

    def test_400_returns_none(self):
        holddown = detect_rate_limit_failure(400, "Bad request", _live_headers())
        assert holddown is None

    def test_429_default_when_no_headers(self):
        holddown = detect_rate_limit_failure(429, "rate limited", {})
        assert holddown == 300.0   # 5-minute default
