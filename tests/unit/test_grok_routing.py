"""Routing-layer tests for grok models.

Two paths converge here:
  - ``_model_family_provider_types("x-ai/grok-4")`` → which provider
    types are eligible? (v3.2.0 added ``grok-web`` to the family.)
  - ``PROVIDER_TYPE_TO_LITELLM`` and ``PROVIDER_DEFAULT_MODELS`` carry
    grok-web entries.

These guard against a class of regression where adding a new provider
type silently drops the family filter and grok-web becomes invisible.
"""
from __future__ import annotations

from app.routing.router import (
    PROVIDER_DEFAULT_MODELS,
    PROVIDER_TYPE_TO_LITELLM,
    _model_family_provider_types,
)


# ── Family filter ──────────────────────────────────────────────────────


def test_xai_slug_includes_grok_web_family():
    """v3.2.0 regression: ``x-ai/grok-4`` requests should include
    ``grok-web`` as an eligible provider type. Pre-v3.2.0 the family
    only listed {grok, openrouter}, so a Grok-Web provider was filtered
    out before scoring even ran."""
    fam = _model_family_provider_types("x-ai/grok-4")
    assert fam is not None
    assert "grok-web" in fam
    assert "grok" in fam      # paid xAI API
    assert "openrouter" in fam  # OpenRouter passthrough


def test_grok_bare_slug_includes_grok_web_family():
    """Bare grok slugs (``grok-3``, ``grok-4``) should also include
    grok-web — operators may not always use the OpenRouter style."""
    fam = _model_family_provider_types("grok-3")
    assert fam is not None
    assert "grok-web" in fam
    assert "grok" in fam


def test_xai_grok_4_fast_in_grok_family():
    """OpenRouter offers grok-4-fast as a separate slug; family filter
    must include grok-web so it's CONSIDERED, even if grok-web's
    capability rows don't list grok-4-fast (then router scoring picks
    OpenRouter naturally — but family filter shouldn't pre-exclude)."""
    fam = _model_family_provider_types("x-ai/grok-4-fast")
    assert fam is not None
    assert "grok-web" in fam


def test_unknown_model_returns_no_family_constraint():
    """Custom finetunes / unknown vendors should return None so the
    capability scorer runs on the full provider set rather than an
    over-restricted subset."""
    assert _model_family_provider_types("my-custom-finetune") is None
    assert _model_family_provider_types("") is None


def test_claude_model_does_not_include_grok_web():
    """Negative case: claude-* must not bleed into the grok family.
    A request for claude-sonnet-4-6 should NOT consider grok-web."""
    fam = _model_family_provider_types("claude-sonnet-4-6")
    assert fam is not None
    assert "grok-web" not in fam
    assert "claude-oauth" in fam


# ── Type ↔ litellm prefix mapping ─────────────────────────────────────


def test_grok_web_in_provider_type_to_litellm():
    """grok-web is a 'never goes through litellm' provider (like
    claude-oauth and codex-oauth), but the entry still has to exist
    so the X-Resolved-Model header can be built without a KeyError.
    Entry maps to xai/ purely for header rendering."""
    assert "grok-web" in PROVIDER_TYPE_TO_LITELLM
    assert PROVIDER_TYPE_TO_LITELLM["grok-web"] == "xai"


def test_grok_web_default_model_is_grok_3():
    """default_model for grok-web = grok-3 (Lite-plan default).
    Operator's Premium plan can override per-Provider, but the default
    matches what most subscriptions grant."""
    assert PROVIDER_DEFAULT_MODELS["grok-web"] == "grok-3"


def test_oauth_subscription_types_present():
    """All three subscription-as-a-provider types must appear in the
    type maps. v3.2.0 added grok-web; v2.7.0 + v3.0.15 added the OAuth
    pair. Missing entries here would cause silent KeyErrors in the
    routing layer."""
    for t in ("claude-oauth", "codex-oauth", "grok-web"):
        assert t in PROVIDER_TYPE_TO_LITELLM, f"{t} missing from type map"
        assert t in PROVIDER_DEFAULT_MODELS, f"{t} missing from default models"
