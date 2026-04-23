"""Unit tests for structured-output JSON-Schema repair loop (Wave 5 #24)."""
import sys
import types

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
sys.modules.setdefault("litellm.exceptions", _stub)

from app.cot.structured_output import (
    extract_json,
    extract_openai_schema,
    extract_anthropic_schema,
    validate_against_schema,
    build_schema_system_prompt,
    build_repair_prompt,
)


class TestExtractJson:
    def test_plain(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_with_fence(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_with_prose_around_braces(self):
        assert extract_json('here you go: {"a": 1} — enjoy') == {"a": 1}

    def test_malformed_returns_none(self):
        assert extract_json("{broken") is None

    def test_empty(self):
        assert extract_json("") is None


class TestExtractOpenAISchema:
    def test_json_object_mode(self):
        body = {"response_format": {"type": "json_object"}}
        assert extract_openai_schema(body) == {"type": "object"}

    def test_json_schema_mode(self):
        body = {"response_format": {"type": "json_schema", "json_schema": {"schema": {"type": "object", "properties": {"x": {"type": "integer"}}}}}}
        assert extract_openai_schema(body) == {"type": "object", "properties": {"x": {"type": "integer"}}}

    def test_none_when_absent(self):
        assert extract_openai_schema({}) is None

    def test_none_for_text_format(self):
        assert extract_openai_schema({"response_format": {"type": "text"}}) is None


class TestExtractAnthropicSchema:
    def test_single_tool_with_tool_choice(self):
        body = {
            "tools": [{"name": "extract", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "extract"},
        }
        assert extract_anthropic_schema(body) == {"type": "object"}

    def test_none_when_multiple_tools(self):
        body = {
            "tools": [{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}],
            "tool_choice": {"type": "any"},
        }
        assert extract_anthropic_schema(body) is None

    def test_none_when_no_tool_choice(self):
        body = {"tools": [{"name": "a", "input_schema": {}}]}
        assert extract_anthropic_schema(body) is None


class TestValidateAgainstSchema:
    def test_passing(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
        assert validate_against_schema({"x": 1}, schema) is None

    def test_missing_required(self):
        schema = {"type": "object", "required": ["x"]}
        err = validate_against_schema({}, schema)
        assert err is not None

    def test_wrong_type(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        err = validate_against_schema({"x": "not an int"}, schema)
        assert err is not None
        assert "x" in err


class TestBuildPrompts:
    def test_system_prompt_includes_schema(self):
        p = build_schema_system_prompt({"type": "object", "properties": {"x": {"type": "integer"}}})
        assert "SINGLE JSON object" in p
        assert "integer" in p

    def test_repair_prompt_includes_error(self):
        p = build_repair_prompt('{"x":"bad"}', "at x: not an integer")
        assert "validation error" in p.lower() or "Validation error" in p
        assert "not an integer" in p
