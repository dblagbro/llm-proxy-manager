"""Unit tests for long-context compression helpers (Wave 5 #26)."""
import sys
import types

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
sys.modules.setdefault("litellm.exceptions", _stub)

from app.api.long_context import (
    estimate_tokens,
    effective_window,
    needs_compression,
    resolve_strategy,
    truncate_to_window,
    _content_to_text,
)


class TestContentToText:
    def test_str_content(self):
        assert _content_to_text("hello") == "hello"

    def test_list_content_text_only(self):
        c = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        assert _content_to_text(c) == "a\nb"

    def test_list_content_skips_non_text(self):
        c = [{"type": "text", "text": "a"}, {"type": "image", "source": {}}]
        assert _content_to_text(c) == "a"


class TestEstimateTokens:
    def test_simple(self):
        msgs = [{"role": "user", "content": "a" * 30}]  # 30 chars / 3 = 10 tokens
        assert estimate_tokens(msgs) == 10

    def test_with_system(self):
        msgs = [{"role": "user", "content": "a" * 30}]
        assert estimate_tokens(msgs, "b" * 15) == 15  # (30+15)/3

    def test_empty(self):
        assert estimate_tokens([]) == 0


class TestEffectiveWindow:
    def test_default_75pct(self):
        assert effective_window(128000) == 96000

    def test_custom_fraction(self):
        assert effective_window(128000, 0.5) == 64000


class TestNeedsCompression:
    def test_small_fits(self):
        msgs = [{"role": "user", "content": "hi"}]
        assert needs_compression(msgs, 128000) is False

    def test_big_doesnt(self):
        # 600k chars / 3 = 200k tokens > 96k (75% of 128k)
        msgs = [{"role": "user", "content": "x" * 600_000}]
        assert needs_compression(msgs, 128000) is True


class TestResolveStrategy:
    def test_default_truncate(self):
        assert resolve_strategy(None) == "truncate"
        assert resolve_strategy("") == "truncate"
        assert resolve_strategy("bogus") == "truncate"

    def test_explicit(self):
        assert resolve_strategy("mapreduce") == "mapreduce"
        assert resolve_strategy("map-reduce") == "mapreduce"
        assert resolve_strategy("error") == "error"
        assert resolve_strategy("TRUNCATE") == "truncate"


class TestTruncateToWindow:
    def test_no_truncation_when_fits(self):
        msgs = [{"role": "user", "content": "short"}] * 3
        out, dropped = truncate_to_window(msgs, 128000)
        assert out == msgs
        assert dropped == 0

    def test_preserves_tail(self):
        # Each message ≈ 100k chars → ~33k tokens → 10 messages > 96k window
        msgs = [{"role": "user", "content": f"m{i}" + "x" * 100_000} for i in range(10)]
        out, dropped = truncate_to_window(msgs, 128000, keep_last=2)
        # Last 2 messages always kept
        assert out[-1] == msgs[-1]
        assert out[-2] == msgs[-2]
        assert dropped > 0

    def test_drops_oldest_first(self):
        msgs = [{"role": "user", "content": f"m{i}" + "x" * 100_000} for i in range(10)]
        out, _ = truncate_to_window(msgs, 128000, keep_last=2)
        # Oldest (m0) should be first to go
        kept_ids = [m["content"][:2] for m in out]
        assert "m0" not in kept_ids[:-2] or "m9" in kept_ids
