"""Unit tests for cascade routing verdict parser (Wave 3 #14)."""
import sys
import types

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
_stub.acompletion = lambda **kwargs: None
sys.modules.setdefault("litellm", _stub)
sys.modules.setdefault("litellm.exceptions", _stub)

from app.routing.cascade import parse_verdict, cascade_requested, CascadeVerdict


class TestParseVerdict:
    def test_clean_accept(self):
        v = parse_verdict('{"acceptable": true, "reason": "looks fine"}')
        assert v.acceptable is True
        assert v.reason == "looks fine"

    def test_clean_reject(self):
        v = parse_verdict('{"acceptable": false, "reason": "missing steps"}')
        assert v.acceptable is False
        assert v.reason == "missing steps"

    def test_markdown_fence_stripped(self):
        v = parse_verdict('```json\n{"acceptable": true, "reason": "ok"}\n```')
        assert v.acceptable is True

    def test_surrounding_prose_ignored(self):
        v = parse_verdict('Sure thing:\n{"acceptable": true, "reason": "good"}\nHope that helps')
        assert v.acceptable is True

    def test_malformed_defaults_to_reject(self):
        v = parse_verdict('{"acceptable": broken')
        assert v.acceptable is False
        assert "parse" in v.reason.lower() or "lenient" in v.reason.lower()

    def test_empty_string_defaults_to_reject(self):
        assert parse_verdict("").acceptable is False


class TestCascadeRequested:
    def test_lmrh_auto(self):
        assert cascade_requested("auto", None) is True

    def test_header_on(self):
        assert cascade_requested(None, "on") is True

    def test_header_true(self):
        assert cascade_requested(None, "true") is True

    def test_neither(self):
        assert cascade_requested(None, None) is False

    def test_off_value(self):
        assert cascade_requested("off", "off") is False

    def test_case_insensitive(self):
        assert cascade_requested("AUTO", None) is True
        assert cascade_requested(None, "ON") is True
