"""Unit tests for PeakEWMA + TTFT hedging logic (Wave 3 #13)."""
import sys
import types
import pytest

# Stub litellm before any app import
sys.modules.setdefault("litellm", types.ModuleType("litellm"))

import app.routing.hedging as hedging_mod
from app.routing.hedging import (
    record_ttft_sample,
    peak_ewma,
    provider_p95_ms,
    should_hedge_header,
    wait_budget_ms,
    _MIN_SAMPLES,
    _WINDOW_SIZE,
)


@pytest.fixture(autouse=True)
def clear_hedging_state():
    """Reset module-level TTFT and PeakEWMA state between tests."""
    hedging_mod._ttft_samples.clear()
    hedging_mod._peak_ewma_ms.clear()
    yield
    hedging_mod._ttft_samples.clear()
    hedging_mod._peak_ewma_ms.clear()


class TestPeakEWMA:
    def test_none_before_any_sample(self):
        assert peak_ewma("p1") is None

    def test_first_sample_initialises_to_that_value(self):
        record_ttft_sample("p1", 100.0)
        assert peak_ewma("p1") == 100.0

    def test_ewma_decays_on_lower_sample(self):
        record_ttft_sample("p1", 200.0)
        record_ttft_sample("p1", 50.0)
        val = peak_ewma("p1")
        assert val is not None
        assert val < 200.0  # decayed
        assert val > 50.0   # still above the low value

    def test_peak_stays_sticky_on_spike(self):
        record_ttft_sample("p1", 100.0)
        record_ttft_sample("p1", 500.0)
        val = peak_ewma("p1")
        # Peak-EWMA biases upward: after a spike the value must be >= the EWMA of 100→500
        assert val is not None
        assert val >= 100.0

    def test_zero_ignored(self):
        record_ttft_sample("p1", 0.0)
        assert peak_ewma("p1") is None

    def test_negative_ignored(self):
        record_ttft_sample("p1", -10.0)
        assert peak_ewma("p1") is None

    def test_window_caps_at_window_size(self):
        for i in range(_WINDOW_SIZE + 50):
            record_ttft_sample("p1", float(i))
        buf = hedging_mod._ttft_samples.get("p1")
        assert buf is not None
        assert len(buf) == _WINDOW_SIZE


class TestP95:
    def test_none_below_min_samples(self):
        for i in range(_MIN_SAMPLES - 1):
            record_ttft_sample("p1", 100.0)
        assert provider_p95_ms("p1") is None

    def test_returns_value_at_min_samples(self):
        for i in range(_MIN_SAMPLES):
            record_ttft_sample("p1", float(i + 1))
        p95 = provider_p95_ms("p1")
        assert p95 is not None

    def test_p95_above_median(self):
        for i in range(100):
            record_ttft_sample("p1", float(i + 1))  # 1..100
        p95 = provider_p95_ms("p1")
        assert p95 is not None
        assert p95 > 50.0  # must be in the upper range

    def test_none_when_never_sampled(self):
        assert provider_p95_ms("unknown") is None


class TestShouldHedgeHeader:
    def test_on_value(self):
        assert should_hedge_header("on", None) is True

    def test_true_value(self):
        assert should_hedge_header("true", None) is True

    def test_one_value(self):
        assert should_hedge_header("1", None) is True

    def test_off_value(self):
        assert should_hedge_header("off", None) is False

    def test_none_header(self):
        assert should_hedge_header(None, None) is False

    def test_lmrh_on(self):
        assert should_hedge_header(None, "on") is True

    def test_lmrh_off(self):
        assert should_hedge_header(None, "off") is False

    def test_header_takes_precedence(self):
        assert should_hedge_header("on", "off") is True


class TestWaitBudget:
    def test_none_when_no_samples(self):
        assert wait_budget_ms("p1") is None

    def test_none_below_threshold(self):
        for _ in range(_MIN_SAMPLES - 1):
            record_ttft_sample("p1", 100.0)
        assert wait_budget_ms("p1") is None

    def test_returns_1_2x_p95(self):
        for i in range(100):
            record_ttft_sample("p1", float(i + 1))
        p95 = provider_p95_ms("p1")
        budget = wait_budget_ms("p1")
        assert budget is not None
        assert abs(budget - p95 * 1.2) < 1e-6
