"""v5.7.22 — UI badge fix: distinguish "auto-rotating healthy" from
"needs operator action" for OAuth providers.

Operator complaint 2026-06-18: Devin-Anthropic-Max providers showed
red "expires in 0d" badge even though v5.7.21 was successfully
rotating their tokens every hour. Root cause: Anthropic claude-oauth
access tokens have a ~7-8h lifetime by design — the badge was
literally correct ("0d" remaining is true) but misleading (token
is being actively rotated, no operator action needed).

This ship updates the frontend ProvidersPage badge logic:
- If has_oauth_refresh_token=true AND no auth_failed → green
  "🔄 auto-rotating (Nh left)" badge instead of red.
- If no refresh token OR auth_failed is set → keep the original
  red/amber alarmist colors (operator action genuinely needed).
"""
from __future__ import annotations

from pathlib import Path


# ── structural pins ────────────────────────────────────────────────────


def test_auto_rotating_branch_in_source():
    """The green-path branch is wired into the badge logic."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "v5.7.22" in src, "v5.7.22 marker missing from ProvidersPage.tsx"
    assert "autoRotating" in src
    # Triggers on has_refresh + NOT auth_failed
    assert "p.has_oauth_refresh_token && !p.auth_failed" in src


def test_green_tone_used_for_auto_rotating():
    """The new badge uses emerald (green) — not amber or red."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    # Capture the autoRotating block boundaries
    idx = src.find("if (autoRotating)")
    assert idx != -1
    # Look at the next 800 chars for the className
    window = src[idx: idx + 800]
    assert "bg-emerald-100" in window, (
        "v5.7.22: auto-rotating badge must use emerald (green) — operator "
        "should see at a glance that no manual action is needed."
    )
    # And NOT red or amber inside the auto-rotating branch
    assert "bg-red-100" not in window
    assert "bg-amber-100" not in window


def test_fallback_red_path_preserved():
    """When refresh-token is absent OR auth_failed is set, the original
    red/amber alarmist badge still fires."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    # After the autoRotating branch's `return`, the original red/amber
    # tone selection must still exist.
    auto_idx = src.find("if (autoRotating)")
    # Find the close of the autoRotating return statement
    fallback_start = src.find("Original", auto_idx)
    assert fallback_start != -1, "Fallback path comment missing"
    fallback_window = src[fallback_start: fallback_start + 1500]
    assert "bg-red-100" in fallback_window
    assert "bg-amber-100" in fallback_window


def test_emoji_is_rotating_arrow():
    """🔄 (rotating-arrow) emoji conveys "actively rotating" semantics.
    Operators read emojis at a glance — 🔄 is unambiguous."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    idx = src.find("if (autoRotating)")
    # Window covers through the close of the JSX return — emoji lives
    # inside the span body, which is past the title attribute.
    window = src[idx: idx + 1500]
    assert "🔄" in window, (
        "v5.7.22: auto-rotating badge should use the 🔄 emoji"
    )


def test_hours_remaining_shown_for_sub_day_tokens():
    """For sub-day tokens (claude-oauth's ~7h lifetime), the badge
    shows hours remaining — operator can see the rotation cadence."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    idx = src.find("if (autoRotating)")
    window = src[idx: idx + 800]
    assert "hours" in window.lower() or "h left" in window
    assert "daysLeft * 24" in window or "daysLeft*24" in window


def test_provider_type_has_required_fields():
    """The TypeScript Provider type carries both
    has_oauth_refresh_token AND auth_failed — required by the new
    badge logic."""
    src = Path("frontend/src/types/index.ts").read_text()
    assert "has_oauth_refresh_token" in src
    assert "auth_failed" in src


def test_version_bumped():
    """v5.7.22 minimum — later patches keep this passing."""
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (5, 7, 22), f"v5.7.22 must be reachable; got {__version__}"
