"""v4.4.40 BUG-086 — cursor-oauth missing from the model-family filter.

Operator-reported 2026-06-03: ``Cursor-oAuth-C1acct`` had priority 4 (lower
number = higher precedence) but ``claude-haiku`` requests were skipping it
in favor of ``Devin-Anthropic-Max-Gmail`` (a higher-priority-number
anthropic-oauth provider). Root cause: ``_model_family_provider_types``
in ``app/routing/litellm_binding.py`` listed only
``{"anthropic", "anthropic-direct", "anthropic-oauth", "claude-oauth"}``
as candidates for ``claude-*`` slugs — ``cursor-oauth`` was excluded, so
the router's family filter eliminated the Cursor provider BEFORE priority
ordering applied.

Same gap existed for the OpenAI family (Cursor's relay also serves
gpt-*, gpt-5, gpt-5-codex). Both fixed in v4.4.40.

Regression that introduced the bug: v4.4.31 (cursor-oauth provider type
added) updated PROVIDER_TYPE_TO_LITELLM + PROVIDER_DEFAULT_MODELS but
forgot the family filter.
"""
from app.routing.litellm_binding import _model_family_provider_types


# ── claude-* family must include cursor-oauth ──────────────────────


def test_claude_family_includes_cursor_oauth():
    for slug in (
        "claude-haiku",
        "claude-3-5-haiku",
        "claude-4-sonnet",
        "claude-4.5-sonnet",
        "claude-4.6-sonnet-medium",
        "claude-opus-4-8-thinking-max",
        "claude/anthropic-haiku",
    ):
        types = _model_family_provider_types(slug)
        assert types is not None, f"{slug!r} should match the Claude family"
        assert "cursor-oauth" in types, (
            f"{slug!r} → {types}: cursor-oauth missing from the Claude family. "
            "The router's pre-priority filter eliminates Cursor providers from "
            "consideration even when their Cursor account can serve the model. "
            "Add cursor-oauth to the claude-* branch of _model_family_provider_types."
        )
        # The pre-existing members must still be present (don't regress).
        assert "anthropic" in types
        assert "claude-oauth" in types


# ── openai/gpt-* family must include cursor-oauth ──────────────────


def test_openai_family_includes_cursor_oauth():
    for slug in (
        "gpt-4o",
        "gpt-5",
        "gpt-5-codex",
        "o1-preview",
        "o3-mini",
        "codex-mini",
    ):
        types = _model_family_provider_types(slug)
        assert types is not None, f"{slug!r} should match the OpenAI family"
        assert "cursor-oauth" in types, (
            f"{slug!r} → {types}: cursor-oauth missing from the OpenAI family. "
            "Cursor's relay serves the gpt-* family too; the family filter "
            "must include cursor-oauth so priority ordering can apply. "
            "Add cursor-oauth to the openai-family branch of _model_family_provider_types."
        )
        assert "openai" in types
        assert "ChatGPT-oauth-plan" in types


# ── other families unaffected (don't over-correct) ─────────────────


def test_grok_family_does_not_include_cursor_oauth():
    """Cursor's catalog doesn't surface xAI's Grok models — keep cursor-oauth
    out of the Grok family so callers asking for ``grok-3`` aren't misrouted."""
    types = _model_family_provider_types("grok-3")
    assert types is not None
    assert "cursor-oauth" not in types


def test_google_family_does_not_include_cursor_oauth():
    """Cursor doesn't relay Google's Gemini models."""
    types = _model_family_provider_types("gemini-2.0-flash")
    assert types is not None
    assert "cursor-oauth" not in types


def test_cohere_family_does_not_include_cursor_oauth():
    """Cursor doesn't relay Cohere embeddings/chat."""
    types = _model_family_provider_types("embed-english-v3.0")
    assert types is not None
    assert "cursor-oauth" not in types


def test_unknown_family_still_returns_none():
    """A model slug we don't recognize must still return None (caller falls
    through to the capability/scoring path). Don't over-restrict legitimate
    custom finetune routes."""
    assert _model_family_provider_types("my-custom-model") is None
    assert _model_family_provider_types("") is None
    assert _model_family_provider_types(None) is None  # type: ignore[arg-type]
