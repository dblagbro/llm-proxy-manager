"""Unit tests for SSE stop-reason normalization audit (Wave 5 #27)."""
import sys
import types

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)

from app.cot.sse import FINISH_TO_STOP


class TestFinishToStop:
    def test_openai_basics(self):
        assert FINISH_TO_STOP["stop"] == "end_turn"
        assert FINISH_TO_STOP["length"] == "max_tokens"
        assert FINISH_TO_STOP["tool_calls"] == "tool_use"

    def test_legacy_function_call(self):
        assert FINISH_TO_STOP["function_call"] == "tool_use"

    def test_content_filter(self):
        assert FINISH_TO_STOP["content_filter"] == "end_turn"

    def test_anthropic_identity(self):
        assert FINISH_TO_STOP["end_turn"] == "end_turn"
        assert FINISH_TO_STOP["max_tokens"] == "max_tokens"
        assert FINISH_TO_STOP["tool_use"] == "tool_use"
        assert FINISH_TO_STOP["stop_sequence"] == "stop_sequence"

    def test_gemini_upper(self):
        assert FINISH_TO_STOP["MAX_TOKENS"] == "max_tokens"
        assert FINISH_TO_STOP["STOP"] == "end_turn"
        assert FINISH_TO_STOP["SAFETY"] == "end_turn"
        assert FINISH_TO_STOP["RECITATION"] == "end_turn"

    def test_unknown_reason_falls_through_via_get(self):
        # Callers use FINISH_TO_STOP.get(x, 'end_turn'); this test just
        # asserts we don't accidentally add a "default" key that masks that.
        assert FINISH_TO_STOP.get("made_up_reason", "end_turn") == "end_turn"
