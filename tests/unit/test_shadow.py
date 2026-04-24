"""Unit tests for shadow-traffic sampling (Wave 3 #16)."""
import sys
import types
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
_stub.aembedding = None
sys.modules.setdefault("litellm", _stub)
# Ensure aembedding exists on whichever stub won (another test may have loaded first)
if not hasattr(sys.modules["litellm"], "aembedding"):
    sys.modules["litellm"].aembedding = None
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.routing.shadow import should_shadow, _embed_cosine


class TestShouldShadow:
    def test_zero_rate_never(self):
        for _ in range(20):
            assert should_shadow(0.0) is False

    def test_full_rate_always(self):
        for _ in range(20):
            assert should_shadow(1.0) is True

    def test_negative_rate_never(self):
        assert should_shadow(-0.5) is False

    def test_above_one_always(self):
        assert should_shadow(1.5) is True

    def test_half_rate_returns_bool(self):
        # Can't assert the exact value, just that it's a bool
        result = should_shadow(0.5)
        assert isinstance(result, bool)


class TestEmbedCosine:
    @pytest.mark.asyncio
    async def test_empty_text_returns_none(self):
        result = await _embed_cosine("", "hello", "text-embedding-3-small", 512)
        assert result is None

    @pytest.mark.asyncio
    async def test_litellm_exception_returns_none(self, monkeypatch):
        """If litellm.aembedding raises, _embed_cosine returns None (never raises)."""
        import app.routing.shadow as shadow_mod
        import litellm

        async def _fail(*args, **kwargs):
            raise RuntimeError("embedding service unavailable")

        monkeypatch.setattr(litellm, "aembedding", _fail)
        result = await _embed_cosine("text a", "text b", "text-embedding-3-small", 512)
        assert result is None

    @pytest.mark.asyncio
    async def test_cosine_perfect_similarity(self, monkeypatch):
        import litellm

        class _FakeResp:
            data = [
                type("D", (), {"embedding": [1.0, 0.0, 0.0]})(),
                type("D", (), {"embedding": [1.0, 0.0, 0.0]})(),
            ]

        async def _fake_embed(*args, **kwargs):
            return _FakeResp()

        monkeypatch.setattr(litellm, "aembedding", _fake_embed)
        result = await _embed_cosine("same", "same", "text-embedding-3-small", 3)
        assert result is not None
        assert abs(result - 1.0) < 1e-9

    @pytest.mark.asyncio
    async def test_cosine_orthogonal_zero(self, monkeypatch):
        import litellm

        class _FakeResp:
            data = [
                type("D", (), {"embedding": [1.0, 0.0]})(),
                type("D", (), {"embedding": [0.0, 1.0]})(),
            ]

        async def _fake_embed(*args, **kwargs):
            return _FakeResp()

        monkeypatch.setattr(litellm, "aembedding", _fake_embed)
        result = await _embed_cosine("a", "b", "text-embedding-3-small", 2)
        assert result is not None
        assert abs(result) < 1e-9
