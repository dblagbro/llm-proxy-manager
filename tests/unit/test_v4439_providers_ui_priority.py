"""v4.4.39 — Providers UI: priority ordinal + 'preferred' badge clarity.

Source-guard tests pinning the operator-filed fix:
- ``priority N`` (bare number) replaced with ``ordinal(N) priority`` (1st, 12th, 112th)
- ``✓ preferred`` (ambiguous — could read as a checkbox) renamed to
  ``🥇 router's pick today`` with an expanded tooltip
- ProviderForm "Priority (lower = preferred)" relabeled to a self-explanatory
  ordinal-aware form: "Priority score (1 = highest, …)"
- A reusable ``ordinal()`` utility was added at ``frontend/src/utils/ordinal.ts``

Background memory: ``project_backlog_providers_ui_priority_preferred``."""
from pathlib import Path


# ── ordinal helper ───────────────────────────────────────────────────


def test_ordinal_helper_exists():
    p = Path("frontend/src/utils/ordinal.ts")
    assert p.exists(), (
        "frontend/src/utils/ordinal.ts missing — the v4.4.39 priority "
        "ordinal-display fix can't work without it"
    )
    src = p.read_text()
    assert "export function ordinal" in src
    # The math handles 11/12/13 via (v - 20) % 10 — pin that's still present.
    assert "v - 20" in src or "v - 20)" in src or "20)" in src


# ── ProvidersPage: priority render uses the ordinal helper ───────────


def test_providers_page_uses_ordinal_for_priority():
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "import { ordinal }" in src or "import {ordinal}" in src, (
        "ProvidersPage.tsx must import the ordinal helper for the priority render"
    )
    # The pre-fix idiom was: `priority {p.priority}`. After the fix, that
    # exact substring should be gone (we render ``{ordinal(p.priority)} priority``).
    assert "priority {p.priority}" not in src, (
        "ProvidersPage.tsx still renders the bare number 'priority {p.priority}'. "
        "Operator-filed 2026-06-03: this is the confusing form that reads as "
        "'higher number = higher priority'. Use ordinal(p.priority) instead."
    )
    # Positive assertion: the ordinal call is present in the render
    assert "ordinal(p.priority)" in src, (
        "ProvidersPage.tsx must call ordinal(p.priority) somewhere in the render"
    )


# ── preferred badge rename ───────────────────────────────────────────


def test_preferred_badge_renamed_to_router_pick():
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    # Old label gone:
    assert "✓ preferred" not in src, (
        "ProvidersPage.tsx still uses the ambiguous '✓ preferred' label "
        "(reads as a checkbox; operator filed 2026-06-03). Rename to the "
        "self-explanatory '🥇 router's pick today' or similar."
    )
    # New label present (any of the proposed forms — operator-flex):
    assert (
        "router&apos;s pick today" in src
        or "router's pick today" in src
        or "router default" in src
        or "lowest util" in src
    ), (
        "ProvidersPage.tsx must use a self-explanatory label for the claude-oauth "
        "auto-preferred badge — not just '✓ preferred'. Suggested: "
        "🥇 router's pick today"
    )


def test_preferred_badge_tooltip_explains_relationship_to_priority():
    """The badge's tooltip must explain it's UNRELATED to the operator-set
    Priority Score field — that's the core source of confusion the operator
    raised on 2026-06-03."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    # Find the badge's title= attribute that describes the preferred semantics.
    # We don't pin the exact words, but it must mention 'priority' OR
    # 'manually-configured' OR 'separate' so the operator hovering it learns
    # the badge ≠ priority field.
    relevant_excerpts = [
        s for s in src.split('title="') if 'router' in s.lower()
    ]
    assert any(
        ("not related" in s.lower()
         or "separate" in s.lower()
         or "manually" in s.lower())
        for s in relevant_excerpts
    ), (
        "The preferred-badge tooltip must explicitly say it's separate from / "
        "not related to the Priority Score field — that's the confusion the "
        "operator surfaced."
    )


# ── ProviderForm: field label clarity ────────────────────────────────


def test_provider_form_priority_label_renamed():
    src = Path("frontend/src/components/providers/ProviderForm.tsx").read_text()
    # Old label gone:
    assert 'label="Priority (lower = preferred)"' not in src, (
        "ProviderForm still uses the old 'Priority (lower = preferred)' label. "
        "Operator filed 2026-06-03: this collides semantically with the "
        "'✓ preferred' badge meaning (which is unrelated). Rename to "
        "'Priority score (1 = highest, …)' or similar."
    )
    # New label uses 'Priority score' to disambiguate:
    assert "Priority score" in src or "priority_score" in src, (
        "ProviderForm must use 'Priority score' (or similar) — disambiguates "
        "from the auto-computed preferred badge."
    )
