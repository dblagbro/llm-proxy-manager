"""Unit tests for retry with exponential backoff (M6)."""
import sys
import types
import asyncio
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
_stub.acompletion = None  # pre-declare for monkeypatch
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})
if not hasattr(sys.modules["litellm"], "acompletion"):
    sys.modules["litellm"].acompletion = None

from app.routing.retry import acompletion_with_retry, _parse_retry_after, _backoff
# Use the same RateLimitError reference that retry.py imported at module-load
import app.routing.retry as _retry_mod
RateLimitError = _retry_mod.RateLimitError


def _exc_with_header(retry_after_value, case="cap"):
    """Build a fake RateLimitError carrying a mock response with Retry-After."""
    class _Resp:
        def __init__(self):
            if case == "cap":
                self.headers = {"Retry-After": str(retry_after_value)}
            elif case == "lower":
                self.headers = {"retry-after": str(retry_after_value)}
            else:
                self.headers = {}

    exc = RateLimitError("rate limited")
    exc.response = _Resp()
    return exc


class TestParseRetryAfter:
    def test_default_when_no_response(self):
        exc = RateLimitError("no headers")
        assert _parse_retry_after(exc) == 5.0

    def test_reads_retry_after_header(self):
        exc = _exc_with_header(10)
        assert _parse_retry_after(exc) == 10.0

    def test_case_insensitive_header(self):
        exc = _exc_with_header(7, case="lower")
        assert _parse_retry_after(exc) == 7.0

    def test_invalid_header_falls_back(self):
        exc = _exc_with_header("not-a-number")
        assert _parse_retry_after(exc) == 5.0

    def test_negative_clamped_to_zero(self):
        exc = _exc_with_header(-5)
        assert _parse_retry_after(exc) == 0.0


class TestBackoff:
    def test_capped_at_max(self):
        # Very large retry_after should be capped at _MAX_WAIT_SEC=30
        result = _backoff(0, 100.0)
        assert result <= 30.0

    def test_scales_with_attempt(self):
        # Attempt 0: min(ra, 1*2)=2 → + jitter
        # Attempt 3: min(ra, 8*2)=16 → + jitter
        a0 = _backoff(0, 100.0)
        a3 = _backoff(3, 100.0)
        assert a3 > a0

    def test_respects_retry_after(self):
        # retry_after=1 should cap the exponential calculation
        for _ in range(10):
            result = _backoff(5, 1.0)
            assert result <= 1.0 * 1.25  # 1 + up to 25% jitter


class TestAcompletionWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_try(self, monkeypatch):
        import litellm

        async def _succeed(model, messages, **kwargs):
            return {"content": "ok"}

        monkeypatch.setattr(litellm, "acompletion", _succeed)
        result = await acompletion_with_retry("gpt-4o", [{"role": "user", "content": "hi"}])
        assert result == {"content": "ok"}

    @pytest.mark.asyncio
    async def test_billing_error_not_retried(self, monkeypatch):
        """Billing-related RateLimitErrors must raise immediately."""
        import litellm
        call_count = {"n": 0}

        async def _always_fail(model, messages, **kwargs):
            call_count["n"] += 1
            raise RateLimitError("insufficient_quota: account has no billing enabled")

        monkeypatch.setattr(litellm, "acompletion", _always_fail)
        with pytest.raises(RateLimitError):
            await acompletion_with_retry("gpt-4o", [{"role": "user", "content": "hi"}], max_retries=3)
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_exhausts_max_retries(self, monkeypatch):
        import litellm
        call_count = {"n": 0}

        async def _always_rate_limit(model, messages, **kwargs):
            call_count["n"] += 1
            raise RateLimitError("429 Too Many Requests")

        async def _fake_sleep(s):
            pass

        monkeypatch.setattr(litellm, "acompletion", _always_rate_limit)
        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

        with pytest.raises(RateLimitError):
            await acompletion_with_retry("gpt-4o", [{"role": "user", "content": "hi"}], max_retries=2)
        assert call_count["n"] == 3

    @pytest.mark.asyncio
    async def test_retry_then_succeeds(self, monkeypatch):
        import litellm
        call_count = {"n": 0}

        async def _flaky(model, messages, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise RateLimitError("rate_limit_error: please back off")
            return {"content": "recovered"}

        async def _fake_sleep(s):
            pass

        monkeypatch.setattr(litellm, "acompletion", _flaky)
        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

        result = await acompletion_with_retry("gpt-4o", [{"role": "user", "content": "hi"}], max_retries=3)
        assert result == {"content": "recovered"}
        assert call_count["n"] == 2


# v3.0.14 — runtime model-deprecation auto-bump
class TestDeprecationAutoBump:
    def test_replacement_for_with_prefix(self):
        from app.routing.retry import _replacement_for
        assert _replacement_for("gemini/gemini-2.0-flash") == "gemini/gemini-2.5-flash"
        assert _replacement_for("anthropic/claude-3-opus-20240229") == "anthropic/claude-opus-4-7"
        assert _replacement_for("totally-fictional-model") is None

    @pytest.mark.asyncio
    async def test_not_found_triggers_bump_and_retry(self, monkeypatch):
        import litellm
        import app.routing.retry as _r
        calls = []

        class _NF(Exception):
            pass
        monkeypatch.setattr(litellm, "NotFoundError", _NF, raising=False)
        monkeypatch.setattr(_r, "NotFoundError", _NF)

        async def _fake_completion(model, messages, **kwargs):
            calls.append(model)
            if model == "gemini/gemini-2.0-flash":
                raise _NF("404 model not found")
            return {"ok": True, "model": model}

        async def _fake_persist(old, new):
            return 1

        monkeypatch.setattr(litellm, "acompletion", _fake_completion)
        monkeypatch.setattr(_r, "_persist_default_model_bump", _fake_persist)

        result = await _r.acompletion_with_retry(
            "gemini/gemini-2.0-flash",
            [{"role": "user", "content": "hi"}],
            max_retries=2,
        )
        assert result == {"ok": True, "model": "gemini/gemini-2.5-flash"}
        assert calls == ["gemini/gemini-2.0-flash", "gemini/gemini-2.5-flash"]

    @pytest.mark.asyncio
    async def test_not_found_without_replacement_reraises(self, monkeypatch):
        import litellm
        import app.routing.retry as _r

        class _NF(Exception):
            pass
        monkeypatch.setattr(litellm, "NotFoundError", _NF, raising=False)
        monkeypatch.setattr(_r, "NotFoundError", _NF)

        async def _fake_completion(model, messages, **kwargs):
            raise _NF("404 totally-fake-model not found")

        monkeypatch.setattr(litellm, "acompletion", _fake_completion)

        with pytest.raises(_NF):
            await _r.acompletion_with_retry(
                "totally-fake-model",
                [{"role": "user", "content": "hi"}],
                max_retries=2,
            )
