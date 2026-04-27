"""Unit tests for the v2.8.0 :floor/:nitro/:exacto slug shortcut parser."""
from __future__ import annotations

from app.routing.model_slug import parse_model_slug


class TestParseModelSlug:
    def test_no_suffix(self):
        r = parse_model_slug("claude-sonnet-4-6")
        assert r.bare_model == "claude-sonnet-4-6"
        assert r.sort_mode is None

    def test_floor_suffix(self):
        r = parse_model_slug("claude-sonnet-4-6:floor")
        assert r.bare_model == "claude-sonnet-4-6"
        assert r.sort_mode == "floor"

    def test_nitro_suffix(self):
        r = parse_model_slug("gpt-4o:nitro")
        assert r.bare_model == "gpt-4o"
        assert r.sort_mode == "nitro"

    def test_exacto_suffix(self):
        r = parse_model_slug("claude-opus-4-7:exacto")
        assert r.bare_model == "claude-opus-4-7"
        assert r.sort_mode == "exacto"

    def test_case_insensitive_suffix(self):
        r = parse_model_slug("model:NITRO")
        assert r.bare_model == "model"
        assert r.sort_mode == "nitro"

    def test_unknown_suffix_passes_through(self):
        # litellm lets you say "anthropic/claude-3-opus:beta" — preserve as-is
        r = parse_model_slug("anthropic/claude-3-opus:beta")
        assert r.bare_model == "anthropic/claude-3-opus:beta"
        assert r.sort_mode is None

    def test_empty_input(self):
        r = parse_model_slug("")
        assert r.bare_model == ""
        assert r.sort_mode is None

    def test_none_input(self):
        r = parse_model_slug(None)
        assert r.bare_model == ""
        assert r.sort_mode is None

    def test_only_suffix(self):
        # ":floor" by itself yields an empty bare model — caller should treat
        # that as a validation error upstream
        r = parse_model_slug(":floor")
        assert r.bare_model == ""
        assert r.sort_mode == "floor"

    def test_composes_with_auto_alias(self):
        # The future "auto" alias should compose: auto:floor → cheapest
        r = parse_model_slug("auto:floor")
        assert r.bare_model == "auto"
        assert r.sort_mode == "floor"

    def test_provider_prefixed_model_with_shortcut(self):
        r = parse_model_slug("openai/gpt-4o:floor")
        assert r.bare_model == "openai/gpt-4o"
        assert r.sort_mode == "floor"


class TestIsAutoModel:
    def test_auto_lowercase(self):
        from app.routing.model_slug import is_auto_model
        assert is_auto_model("auto") is True

    def test_auto_uppercase(self):
        from app.routing.model_slug import is_auto_model
        assert is_auto_model("AUTO") is True

    def test_llmp_auto(self):
        from app.routing.model_slug import is_auto_model
        assert is_auto_model("llmp-auto") is True

    def test_real_model(self):
        from app.routing.model_slug import is_auto_model
        assert is_auto_model("claude-sonnet-4-6") is False

    def test_empty(self):
        from app.routing.model_slug import is_auto_model
        assert is_auto_model("") is False
        assert is_auto_model(None) is False

    def test_auto_with_suffix_must_be_parsed_first(self):
        # is_auto_model expects the suffix-stripped bare model
        from app.routing.model_slug import is_auto_model, parse_model_slug
        # "auto:floor" stays a non-auto string until parse_model_slug strips it
        assert is_auto_model("auto:floor") is False
        # After parsing → bare="auto" is auto
        assert is_auto_model(parse_model_slug("auto:floor").bare_model) is True
