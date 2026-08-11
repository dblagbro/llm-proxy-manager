"""v5.22.7 regression: litellm gets OpenAI-shaped tools.

Production 2026-08-11: Devin-Cohere's breaker sat open with every
tool-carrying request failing::

    CohereException - invalid tool at tools[0]: missing required field: 'type'

/v1/messages accepts Anthropic-format tools ({name, description,
input_schema}) and passed them to litellm verbatim; litellm's provider
adapters expect the OpenAI shape ({type:'function', function:{...}}), so the
`type` discriminator was absent.

The claude-oauth path does its own Anthropic->OpenAI conversion, so the
normalizer must be idempotent or that path would be double-converted into
empty-named tools.
"""
import sys
import types

sys.modules.setdefault("litellm", types.ModuleType("litellm"))

from app.api._oauth_chat_translate import normalize_tools_for_litellm  # noqa: E402

ANTHROPIC_TOOL = {
    "name": "get_weather",
    "description": "Look up the weather",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}
OPENAI_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up the weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


class TestAnthropicShapeIsConverted:
    def test_type_field_is_present(self):
        """The exact field Cohere rejected."""
        out = normalize_tools_for_litellm([ANTHROPIC_TOOL])
        assert out[0]["type"] == "function"

    def test_schema_is_carried_over_to_parameters(self):
        out = normalize_tools_for_litellm([ANTHROPIC_TOOL])
        fn = out[0]["function"]
        assert fn["name"] == "get_weather"
        assert fn["description"] == "Look up the weather"
        assert fn["parameters"]["properties"]["city"]["type"] == "string"

    def test_every_tool_in_a_list_is_converted(self):
        out = normalize_tools_for_litellm([ANTHROPIC_TOOL, dict(ANTHROPIC_TOOL, name="b")])
        assert [t["type"] for t in out] == ["function", "function"]
        assert [t["function"]["name"] for t in out] == ["get_weather", "b"]


class TestIdempotence:
    def test_openai_shape_passes_through_unchanged(self):
        out = normalize_tools_for_litellm([OPENAI_TOOL])
        assert out == [OPENAI_TOOL]

    def test_double_normalizing_does_not_blank_the_name(self):
        """Regression guard: a second pass must not produce name=''."""
        once = normalize_tools_for_litellm([ANTHROPIC_TOOL])
        twice = normalize_tools_for_litellm(once)
        assert twice == once
        assert twice[0]["function"]["name"] == "get_weather"

    def test_mixed_shapes_both_end_up_openai(self):
        out = normalize_tools_for_litellm([ANTHROPIC_TOOL, OPENAI_TOOL])
        assert len(out) == 2
        assert all(t.get("type") == "function" for t in out)
        assert all(isinstance(t.get("function"), dict) for t in out)


class TestEmptyAndJunk:
    def test_none_and_empty_pass_through(self):
        assert normalize_tools_for_litellm(None) is None
        assert normalize_tools_for_litellm([]) == []

    def test_non_dict_entries_are_dropped(self):
        out = normalize_tools_for_litellm(["nonsense", None, ANTHROPIC_TOOL])
        assert len(out) == 1
        assert out[0]["function"]["name"] == "get_weather"

    def test_missing_input_schema_still_yields_valid_tool(self):
        out = normalize_tools_for_litellm([{"name": "noargs"}])
        assert out[0]["type"] == "function"
        assert out[0]["function"]["parameters"]["type"] == "object"


class TestHandlerWiring:
    """messages.py must send the NORMALIZED list to litellm, and keep the raw
    Anthropic `tools` for the claude-oauth path."""

    def _src(self):
        from pathlib import Path
        return Path("app/api/messages.py").read_text(encoding="utf-8")

    def test_normalized_variable_is_built(self):
        assert "litellm_tools = normalize_tools_for_litellm(tools)" in self._src()

    def test_no_litellm_extra_still_uses_raw_tools(self):
        src = self._src()
        for bad in ('extra["tools"] = tools',
                    'b_extra["tools"] = tools',
                    '_e["tools"] = tools',
                    'local_extra["tools"] = tools'):
            assert bad not in src, f"{bad} still passes Anthropic-shaped tools to litellm"

    def test_all_four_handoffs_use_the_normalized_list(self):
        assert self._src().count('"tools"] = litellm_tools') == 4
