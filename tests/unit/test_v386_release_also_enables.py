"""v3.8.6 — release_manual_overrides also re-enables by default.

Operator-reported UX bug: clicking the banner's release button left
providers in enabled=False state, the provider detail then showed an
"Enable" button, and the operator was unsure whether their click had
taken effect.

Root cause: the v3.7.28 design said "release just clears the lock; the
AI supervisor will manage enabled from there". But the supervisor is
opt-in and currently off, so release-only left providers stranded.

Fix: ``release_manual_overrides`` now ALSO sets enabled=True by default
(symmetric to the Disable click that originally locked them). Caller
can opt out via ``?enable=false`` for the legacy release-only behavior.
"""
from __future__ import annotations

from pathlib import Path


def test_release_endpoint_re_enables_by_default():
    src = Path("app/api/providers.py").read_text()
    idx = src.index("async def release_manual_overrides")
    body = src[idx:idx + 3000]
    # Default behavior: enable=True
    assert "enable: bool = True" in body
    # When enable is set, values include enabled=True
    assert 'values["enabled"] = True' in body
    # Response surfaces the enable flag so callers know what happened
    assert '"re_enabled": enable' in body


def test_release_endpoint_supports_opt_out():
    """Caller can pass ?enable=false to get pre-v3.8.6 release-only
    behavior (e.g. an operator script handing off to AI supervisor)."""
    src = Path("app/api/providers.py").read_text()
    idx = src.index("async def release_manual_overrides")
    body = src[idx:idx + 3000]
    # The conditional path: skip the enabled column when caller opts out
    assert "if enable:" in body


def test_banner_button_label_changed_to_clear_phrasing():
    """Pre-v3.8.6: 'Release all to AI control' — implied AI was about to
    manage things, hid the fact that providers stay disabled.
    Post-v3.8.6: 'Release & re-enable all' — explicit about both
    effects."""
    src = Path("frontend/src/components/layout/ManualOverrideBanner.tsx").read_text()
    # Old label gone
    assert "Release all to AI control" not in src
    # New label present (HTML-encoded & for JSX)
    assert "Release &amp; re-enable all" in src


def test_banner_confirmation_prompt_spells_out_action():
    """The confirm-step prompt + button label spell out 'release &
    re-enable' so operators know what 'Yes' will do. JSX renders
    ``&amp;`` in text content but plain ``&`` inside curly-brace JS
    expressions, so the test accepts either form."""
    src = Path("frontend/src/components/layout/ManualOverrideBanner.tsx").read_text()
    assert "Yes, release & enable" in src or "Yes, release &amp; enable" in src
    assert "Release locks &amp; re-enable" in src


def test_banner_toast_surfaces_re_enable_flag():
    """The success toast tells the operator whether re-enable happened
    (default true) or was opted out of. Removes the ambiguity that
    caused the original confusion."""
    src = Path("frontend/src/components/layout/ManualOverrideBanner.tsx").read_text()
    assert "re_enabled" in src
    assert "releases + re-enables" in src or "re-enables" in src


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 8, 6)
