"""v5.22.20 — CoT's internal calls must be counted, and auth-skip must persist.

**Cost accounting.** Each CoT iteration issues its own upstream completion via
``_call()``. Those bill real tokens, but they are never streamed to the
client, so they produce no ``message_delta`` and the metrics wrapper never saw
them. That wrapper also *assigns* rather than accumulates, so what reached
``provider_metrics`` was the final answer's usage and nothing else.

Measured against the vendor dashboard for one key over Sep 09-16:
**4,354,580 input tokens billed vs 2,629,493 recorded** — 40% low, and spend
37% low for the month. That is why per-key spending caps could not be trusted:
a cap enforced on a 40%-low count lets real spend run well past it.

**Auth-skip persistence.** ``record_auth_failure`` sets a 24h hold-down
unconditionally, but escalation to the DB-persisted ``auto_skip_until`` used
to need 3 failures. Once the breaker opens the provider leaves the routing
pool, so failures 2 and 3 could not happen for another day — the two
mechanisms fought each other. Observed live: one failure marked, then nothing
for 24h, with ``auto_skip_until`` stale for a month.
"""

import pytest

from app.cot import usage as cot


class _Usage:
    def __init__(self, prompt, completion):
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _AltUsage:
    """Some providers spell it input_tokens/output_tokens."""

    def __init__(self, prompt, completion):
        self.input_tokens = prompt
        self.output_tokens = completion


class _Resp:
    def __init__(self, usage):
        self.usage = usage


@pytest.fixture(autouse=True)
def _fresh():
    cot.reset_cot_usage()
    yield
    cot.reset_cot_usage()


class TestAccumulation:
    def test_starts_empty(self):
        assert cot.get_cot_usage() == {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def test_accumulates_across_iterations(self):
        """cot_max_iterations defaults to 4 — the whole point is that every
        pass is counted, not just the last."""
        for _ in range(4):
            cot.accumulate_usage(_Resp(_Usage(1200, 40)))
        assert cot.get_cot_usage() == {
            "input_tokens": 4800,
            "output_tokens": 160,
            "calls": 4,
        }

    def test_handles_the_alternate_usage_spelling(self):
        cot.accumulate_usage(_Resp(_AltUsage(900, 30)))
        u = cot.get_cot_usage()
        assert u["input_tokens"] == 900 and u["output_tokens"] == 30

    def test_response_without_usage_is_ignored_not_fatal(self):
        cot.accumulate_usage(_Resp(None))
        cot.accumulate_usage(object())
        assert cot.get_cot_usage()["calls"] == 0

    def test_reset_isolates_requests(self):
        cot.accumulate_usage(_Resp(_Usage(500, 10)))
        cot.reset_cot_usage()
        assert cot.get_cot_usage()["calls"] == 0

    def test_get_returns_a_copy(self):
        """A caller mutating the result must not corrupt the accumulator."""
        cot.accumulate_usage(_Resp(_Usage(100, 5)))
        snapshot = cot.get_cot_usage()
        snapshot["input_tokens"] = 999_999
        assert cot.get_cot_usage()["input_tokens"] == 100


class TestFoldIn:
    def test_adds_rather_than_overwrites(self):
        """The caller's own totals come from message_delta, which is
        cumulative for ONE message. CoT calls are extra upstream calls."""
        for _ in range(3):
            cot.accumulate_usage(_Resp(_Usage(1000, 25)))
        assert cot.fold_cot_usage(500, 20) == (3500, 95)

    def test_no_cot_calls_leaves_totals_untouched(self):
        """A non-CoT request must record exactly what it always did."""
        assert cot.fold_cot_usage(742, 31) == (742, 31)

    def test_recovers_the_measured_shortfall(self):
        """Sanity-check the fix against the real discrepancy: recorded 2.63M
        against 4.35M billed is a 1.66x multiplier."""
        recorded = 2_629_493
        cot.accumulate_usage(_Resp(_Usage(1_725_087, 0)))
        folded_in, _ = cot.fold_cot_usage(recorded, 0)
        assert folded_in == 4_354_580


class TestWiring:
    def test_streaming_wrapper_resets_and_folds(self):
        src = __import__("pathlib").Path("app/api/_messages_streaming.py").read_text()
        assert "reset_cot_usage()" in src, (
            "accumulator never reset — usage would leak between requests"
        )
        assert "fold_cot_usage(in_tok, out_tok" in src, "internal usage never folded in"
        reset_at = src.index("reset_cot_usage()")
        fold_at = src.index("fold_cot_usage(in_tok")
        assert reset_at < fold_at, "fold happens before reset"


class TestAuthSkipPersistsImmediately:
    @pytest.mark.asyncio
    async def test_first_auth_failure_persists(self, monkeypatch):
        """The breaker already applies 24h on failure #1; writing it down at
        the same moment adds no aggressiveness, and makes it survive a
        restart."""
        from app.routing import circuit_breaker as cb

        calls = []

        async def _fake_persist(provider_id, error_text):
            calls.append(provider_id)

        monkeypatch.setattr(cb, "_persist_auto_skip", _fake_persist)
        cb.clear_auth_failure("p-new")
        await cb.record_auth_failure("p-new", "401 invalid api key")
        assert calls == ["p-new"]
        cb.clear_auth_failure("p-new")
