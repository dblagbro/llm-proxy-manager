"""Unit tests for CoT session store (Redis with in-memory fallback)."""
import sys
import types
import time
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

# A sibling test (test_cot_pipeline) stubs app.cot.session in sys.modules to
# stop heavy imports. Force a fresh import here so we exercise the real module.
sys.modules.pop("app.cot.session", None)
import importlib
session_mod = importlib.import_module("app.cot.session")
get_session_analyses = session_mod.get_session_analyses
save_session_analysis = session_mod.save_session_analysis
_clean_fallback = session_mod._clean_fallback


@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    """Reset module-level state and force redis-unavailable for every test."""
    session_mod._fallback.clear()
    session_mod._redis_client = None
    session_mod._redis_ok = False
    # Force the redis path to return None (fallback used)
    async def _no_redis():
        return None
    monkeypatch.setattr(session_mod, "_get_redis", _no_redis)
    yield
    session_mod._fallback.clear()


class TestGetSessionAnalyses:
    @pytest.mark.asyncio
    async def test_none_session_id_returns_empty(self):
        assert await get_session_analyses(None) == []

    @pytest.mark.asyncio
    async def test_empty_string_session_id_returns_empty(self):
        assert await get_session_analyses("") == []

    @pytest.mark.asyncio
    async def test_unknown_session_returns_empty(self):
        assert await get_session_analyses("never-seen") == []

    @pytest.mark.asyncio
    async def test_round_trip_single_analysis(self):
        await save_session_analysis("sess-1", "analysis one")
        got = await get_session_analyses("sess-1")
        assert got == ["analysis one"]

    @pytest.mark.asyncio
    async def test_round_trip_multiple_analyses(self):
        await save_session_analysis("sess-2", "first")
        await save_session_analysis("sess-2", "second")
        await save_session_analysis("sess-2", "third")
        got = await get_session_analyses("sess-2")
        assert got == ["first", "second", "third"]


class TestSaveSessionAnalysis:
    @pytest.mark.asyncio
    async def test_none_session_id_noop(self):
        await save_session_analysis(None, "hello")
        assert session_mod._fallback == {}

    @pytest.mark.asyncio
    async def test_empty_analysis_noop(self):
        await save_session_analysis("sess-x", "")
        assert session_mod._fallback == {}

    @pytest.mark.asyncio
    async def test_caps_at_max_analyses(self, monkeypatch):
        """When max is 3, the 4th save should drop the oldest entry."""
        from app.config import settings
        monkeypatch.setattr(settings, "cot_session_max_analyses", 3)

        for a in ["one", "two", "three", "four"]:
            await save_session_analysis("sess-cap", a)

        got = await get_session_analyses("sess-cap")
        assert got == ["two", "three", "four"]
        assert len(got) == 3

    @pytest.mark.asyncio
    async def test_per_session_isolation(self):
        await save_session_analysis("a", "apple")
        await save_session_analysis("b", "banana")
        assert await get_session_analyses("a") == ["apple"]
        assert await get_session_analyses("b") == ["banana"]


class TestTTLFallback:
    @pytest.mark.asyncio
    async def test_expired_session_returns_empty(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "cot_session_ttl_sec", 60)
        await save_session_analysis("sess-ttl", "stale")
        # Manually backdate the timestamp
        session_mod._fallback["sess-ttl"]["ts"] = time.time() - 3600
        got = await get_session_analyses("sess-ttl")
        assert got == []

    def test_clean_fallback_removes_expired(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "cot_session_ttl_sec", 60)
        now = time.time()
        session_mod._fallback["fresh"] = {"analyses": ["a"], "ts": now - 10}
        session_mod._fallback["stale"] = {"analyses": ["b"], "ts": now - 3600}
        _clean_fallback()
        assert "fresh" in session_mod._fallback
        assert "stale" not in session_mod._fallback
