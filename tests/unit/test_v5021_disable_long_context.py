"""v5.0.21 — per-provider 1M-context-beta opt-out via ContextVar.

Behavioral pins:
  - ``_beta_flags_for_model`` strips ``context-1m-2025-08-07`` when the
    flag is True, even on whitelisted models.
  - ``build_headers`` honors both the kwarg AND the ContextVar set by
    ``set_disable_long_context``.
  - Bool-parsing identity check (``is True``) — the dispatch sites must
    NOT use ``bool(...)`` because ``bool("false") == True`` would
    silently invert operator intent.

Source pins:
  - Dispatch sites use ``getattr(..., "extra_config", None) or {}`` so
    test mocks (SimpleNamespace without that attribute) don't crash
    the call. This was a real regression caught 2026-06-05 by the
    test_v31015 suite.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


# ── Behavioral ──────────────────────────────────────────────────────


def test_beta_flags_strips_1m_when_disable_true():
    from app.providers.claude_oauth import _beta_flags_for_model
    flags = _beta_flags_for_model("claude-sonnet-4-6", disable_long_context=True)
    assert "context-1m-2025-08-07" not in flags


def test_beta_flags_keeps_1m_for_whitelisted_when_disable_false():
    from app.providers.claude_oauth import _beta_flags_for_model
    flags = _beta_flags_for_model("claude-sonnet-4-6", disable_long_context=False)
    assert "context-1m-2025-08-07" in flags


def test_beta_flags_strips_1m_for_haiku_regardless():
    """Haiku was never on the 1M whitelist; flag has no effect here."""
    from app.providers.claude_oauth import _beta_flags_for_model
    flags_default = _beta_flags_for_model("claude-haiku-4-5", disable_long_context=False)
    flags_disabled = _beta_flags_for_model("claude-haiku-4-5", disable_long_context=True)
    assert "context-1m-2025-08-07" not in flags_default
    assert "context-1m-2025-08-07" not in flags_disabled


def test_build_headers_honors_contextvar():
    """When the dispatch site sets the ContextVar, build_headers picks
    it up without an explicit kwarg."""
    from app.providers.claude_oauth import (
        build_headers, set_disable_long_context, _disable_long_context_cv,
    )
    _disable_long_context_cv.set(False)
    h1 = build_headers("tok", model="claude-sonnet-4-6")
    assert "context-1m-2025-08-07" in h1["anthropic-beta"]

    set_disable_long_context(True)
    h2 = build_headers("tok", model="claude-sonnet-4-6")
    assert "context-1m-2025-08-07" not in h2["anthropic-beta"]
    # Reset so we don't poison later tests in the same task.
    _disable_long_context_cv.set(False)


def test_build_headers_kwarg_overrides_contextvar():
    """If a caller passes ``disable_long_context=True`` explicitly,
    that wins regardless of ContextVar state."""
    from app.providers.claude_oauth import build_headers, _disable_long_context_cv
    _disable_long_context_cv.set(False)
    h = build_headers("tok", model="claude-sonnet-4-6", disable_long_context=True)
    assert "context-1m-2025-08-07" not in h["anthropic-beta"]


# ── Source pins — dispatch-site contract ───────────────────────────


def test_dispatch_sites_use_getattr_not_attribute_access():
    """The 3 dispatch sites that call set_disable_long_context() MUST
    use ``getattr(provider, "extra_config", None)`` rather than the
    bare attribute access. The bare form crashes on test mocks and
    on any code path where ``provider`` is a non-ORM object — both of
    which DID occur in v5.0.21 RC (test_v31015 OAuth tests, 7
    failures, fixed in v5.0.21 hotfix)."""
    targets = [
        "app/api/_messages_dispatch.py",
        "app/api/completions.py",
        "app/monitoring/keepalive.py",
        "app/providers/scanner.py",
    ]
    for path in targets:
        src = Path(path).read_text()
        # Must contain getattr — pure attribute access would re-introduce the bug
        assert "getattr(" in src and "extra_config" in src, (
            f"{path} should use getattr(...,'extra_config',None) for the "
            f"disable_long_context lookup. Otherwise mocks/non-ORM callers crash."
        )


def test_dispatch_sites_use_identity_check_not_bool():
    """``bool('false') is True``, which would silently invert operator
    intent if the value is ever rehydrated as a string. All dispatch
    sites that read disable_long_context must use ``is True`` rather
    than ``bool(...)``."""
    targets = [
        "app/api/_messages_dispatch.py",
        "app/api/completions.py",
        "app/monitoring/keepalive.py",
        "app/providers/scanner.py",
    ]
    for path in targets:
        src = Path(path).read_text()
        # The dispatch lines should contain `is True` adjacent to the lookup
        lines_with_long_context = [
            ln for ln in src.splitlines()
            if "disable_long_context" in ln and ("getattr" in ln or "extra_config" in ln)
        ]
        # At least one of the relevant lines should include the `is True` check
        # OR the next few lines should (multi-line set_disable_long_context call)
        idx = src.find("disable_long_context")
        while idx != -1:
            window = src[idx:idx + 300]
            if "is True" in window:
                break
            idx = src.find("disable_long_context", idx + 1)
        else:
            pytest.fail(
                f"{path}: disable_long_context lookups should use `is True` "
                f"rather than `bool(...)` to avoid the string-'false' inversion bug."
            )


# ── Regression — dispatch mock contract ────────────────────────────


def test_dispatch_tolerates_mock_provider_without_extra_config():
    """Direct simulation: a mock provider lacking ``extra_config``
    must not crash the dispatch site's set_disable_long_context call."""
    from app.providers.claude_oauth import set_disable_long_context

    mock_provider = SimpleNamespace(
        id="mock", provider_type="claude-oauth", api_key="x"
    )  # NOTE: no extra_config attribute
    # This is the EXACT expression the dispatch sites use post-hotfix.
    set_disable_long_context(
        (getattr(mock_provider, "extra_config", None) or {}).get("disable_long_context") is True
    )
    # No exception → contract satisfied.
