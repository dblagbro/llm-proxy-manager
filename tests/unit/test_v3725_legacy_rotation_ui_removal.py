"""v3.7.25 (#257) — Remove the stale "Usage-based rotation
(disabled — External Usage above is authoritative)" subsection from the
provider detail form for claude-oauth.

Source-level checks since this is a frontend-only change and there is
no live React harness in this repo. The behaviors verified:

  1. The "Usage-based rotation" block is rendered only when
     provider_type != 'claude-oauth' (removed unconditionally for
     claude-oauth, no disclosure left behind).
  2. The "Show legacy fields (advanced)" disclosure button is gone.
  3. The local ``showLegacyUsage`` state is gone.
  4. The "(disabled — External Usage above is authoritative)" sub-label
     is gone.
  5. The forward-reference in AnthropicBillingPanel ("supersedes the
     proxy-internal 'Usage-based rotation' section below") is cleaned up
     since there's no such section to reference for claude-oauth.
"""
from __future__ import annotations

from pathlib import Path


def _form_src() -> str:
    return Path("frontend/src/components/providers/ProviderForm.tsx").read_text()


def _panel_src() -> str:
    return Path("frontend/src/components/providers/AnthropicBillingPanel.tsx").read_text()


def test_show_legacy_usage_state_removed():
    src = _form_src()
    assert "showLegacyUsage" not in src, (
        "showLegacyUsage state and references must be removed when the "
        "section it gated is removed for claude-oauth."
    )


def test_disabled_external_usage_label_removed():
    src = _form_src()
    assert "(disabled — External Usage above is authoritative)" not in src
    assert "Show legacy fields (advanced)" not in src
    assert "Hide legacy fields" not in src


def test_rotation_section_gated_on_non_claude_oauth():
    """The 'Usage-based rotation' block must be conditionally rendered —
    only for non-claude-oauth providers (e.g. codex-oauth and below)."""
    src = _form_src()
    # The conditional wrapper guards the entire section
    assert "form.provider_type !== 'claude-oauth'" in src
    # And the heading is still emitted (for non-claude-oauth providers)
    assert "Usage-based rotation" in src


def test_section_not_shown_for_claude_oauth():
    """Source-level: when traversing from the AnthropicBillingPanel
    region down to the rotation section, the conditional check must be
    in between so React skips the subtree for claude-oauth."""
    src = _form_src()
    panel_idx = src.index("AnthropicBillingPanel")
    rotation_idx = src.index("Usage-based rotation")
    assert panel_idx < rotation_idx
    between = src[panel_idx:rotation_idx]
    assert "form.provider_type !== 'claude-oauth'" in between


def test_panel_no_longer_forward_references_legacy_section():
    """The Anthropic billing panel previously said 'supersedes the
    proxy-internal Usage-based rotation section below'. Since the
    legacy section no longer renders for claude-oauth, that forward-
    reference is removed/rewritten."""
    src = _panel_src()
    # The old "supersedes the proxy-internal 'Usage-based rotation'
    # section below" line is gone (or no longer points at a section
    # that exists for claude-oauth).
    assert "section below" not in src


def test_panel_still_describes_rotation_signal_role():
    """Sanity: the AnthropicBillingPanel still tells the operator that
    this panel IS the rotation signal — we removed only the forward
    reference, not the affirmative statement."""
    src = _panel_src()
    assert "rotation signal" in src
    assert "claude-oauth" in src


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 25)
