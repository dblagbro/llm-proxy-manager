"""Unit tests for _check_rpd / _check_burst / in-flight tracking (Wave 6)."""
import sys
import types
import time
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from fastapi import HTTPException

import app.auth.keys as keys_mod
import app.auth.rate_limit_state as rl_state  # monkeypatch target after 2026-04-23 refactor
from app.auth.keys import (
    _check_rpd, _check_burst, begin_in_flight, end_in_flight,
)


@pytest.fixture(autouse=True)
def reset_state():
    keys_mod._rpd_buckets.clear()
    keys_mod._burst_counters.clear()
    yield
    keys_mod._rpd_buckets.clear()
    keys_mod._burst_counters.clear()


@pytest.fixture
def single_node(monkeypatch):
    """Force active_node_count=1 so per-node share == full limit."""
    monkeypatch.setattr(rl_state, "active_node_count", lambda: 1)


class TestCheckRpd:
    def test_first_request_passes(self, single_node):
        _check_rpd("k1", 10)  # should not raise

    def test_under_limit_passes(self, single_node):
        for _ in range(9):
            _check_rpd("k1", 10)

    def test_exceeds_limit_raises_429(self, single_node):
        for _ in range(10):
            _check_rpd("k1", 10)
        with pytest.raises(HTTPException) as exc_info:
            _check_rpd("k1", 10)
        assert exc_info.value.status_code == 429
        assert "Daily" in exc_info.value.detail or "daily" in exc_info.value.detail.lower()

    def test_retry_after_header_is_long(self, single_node):
        for _ in range(5):
            _check_rpd("k1", 5)
        with pytest.raises(HTTPException) as exc_info:
            _check_rpd("k1", 5)
        ra = exc_info.value.headers.get("Retry-After")
        assert int(ra) >= 60  # daily limit -> Retry-After should be 1h+

    def test_per_key_isolation(self, single_node):
        for _ in range(5):
            _check_rpd("k1", 5)
        # k1 is now at limit, but k2 should still have its own fresh bucket
        _check_rpd("k2", 5)

    def test_bucket_resets_when_day_changes(self, single_node, monkeypatch):
        # Exhaust limit on day 0
        for _ in range(3):
            _check_rpd("k1", 3)
        with pytest.raises(HTTPException):
            _check_rpd("k1", 3)

        # Manually mark bucket as yesterday
        keys_mod._rpd_buckets["k1"] = (keys_mod._rpd_buckets["k1"][0] - 1, 3)
        # Next request should reset bucket and succeed
        _check_rpd("k1", 3)

    def test_per_node_share_splits_limit(self, monkeypatch):
        """With 3 nodes, limit=300 => per-node limit=100."""
        monkeypatch.setattr(rl_state, "active_node_count", lambda: 3)
        for _ in range(100):
            _check_rpd("k1", 300)
        with pytest.raises(HTTPException):
            _check_rpd("k1", 300)


class TestCheckBurst:
    def test_zero_in_flight_passes(self):
        _check_burst("k1", 5)

    def test_under_limit_passes(self):
        begin_in_flight("k1")
        begin_in_flight("k1")
        _check_burst("k1", 5)  # 2 in flight, limit 5

    def test_at_limit_raises(self):
        for _ in range(5):
            begin_in_flight("k1")
        with pytest.raises(HTTPException) as exc_info:
            _check_burst("k1", 5)
        assert exc_info.value.status_code == 429
        assert "concurren" in exc_info.value.detail.lower() or "in-flight" in exc_info.value.detail.lower()

    def test_end_in_flight_frees_slot(self):
        for _ in range(5):
            begin_in_flight("k1")
        with pytest.raises(HTTPException):
            _check_burst("k1", 5)
        end_in_flight("k1")
        _check_burst("k1", 5)  # should pass now


class TestInFlightCounter:
    def test_begin_increments(self):
        begin_in_flight("k1")
        assert keys_mod._burst_counters["k1"] == 1
        begin_in_flight("k1")
        assert keys_mod._burst_counters["k1"] == 2

    def test_end_decrements(self):
        begin_in_flight("k1")
        begin_in_flight("k1")
        end_in_flight("k1")
        assert keys_mod._burst_counters["k1"] == 1

    def test_end_never_goes_negative(self):
        end_in_flight("k1")  # no begin
        end_in_flight("k1")
        assert keys_mod._burst_counters.get("k1", 0) == 0

    def test_per_key_isolation(self):
        begin_in_flight("k1")
        begin_in_flight("k1")
        begin_in_flight("k2")
        assert keys_mod._burst_counters["k1"] == 2
        assert keys_mod._burst_counters["k2"] == 1
