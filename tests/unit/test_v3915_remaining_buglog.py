"""v3.9.15 — BUG-007 + BUG-012 fixes from the 2026-04-24 bug-log sweep.

After audit, most April-24 sweep items were already closed in
v2.7.6 → v3.9.14:
  BUG-002, BUG-003, BUG-004, BUG-005, BUG-006, BUG-008, BUG-009,
  BUG-010 (backend), BUG-013, BUG-014, BUG-015, BUG-016, BUG-017,
  BUG-018, BUG-019 — all FIXED.

What v3.9.15 closes:
  BUG-007: rename ``refresh_access_token`` to
           ``_internal_refresh_access_token`` (+ deprecation alias for
           one release).
  BUG-012: ``--skip-destructive`` flag on the burn test so weekly
           automated runs don't rotate the live refresh token.

What stays open (deferred with justification):
  BUG-001: streaming-error contract redesign needs cross-team coordination
           with DevinGPT/hub before changing wire behavior.
  ARCH-A:  latent DB connection leak — audit shows every
           AsyncSessionLocal() call is inside `async with`, so the leak
           isn't from naive unmanaged sessions. Needs more diagnostic
           data from the next live recurrence to localize.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest


# ── BUG-007: rename + deprecation alias ────────────────────────────


def test_internal_refresh_helper_exists():
    """The renamed canonical helper must exist."""
    from app.providers.claude_oauth_flow import _internal_refresh_access_token
    assert asyncio.iscoroutinefunction(_internal_refresh_access_token)


def test_old_name_still_importable_as_alias():
    """One-release back-compat: the old import path still works so we
    don't break callers in flight (the burn test is the only known one;
    migrated in the same release)."""
    from app.providers.claude_oauth_flow import refresh_access_token
    assert asyncio.iscoroutinefunction(refresh_access_token)


def test_old_name_emits_deprecation_warning():
    """The alias must warn the caller so they migrate."""
    import warnings
    from app.providers.claude_oauth_flow import refresh_access_token

    # Capture warnings without actually doing the HTTP call — the
    # warning fires BEFORE the network attempt because we call
    # warnings.warn at the top of the function. Pass an obviously-bogus
    # token + cancel before httpx fires.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        coro = refresh_access_token("bogus-test-token")
        try:
            # Get the warning to fire; coroutine returns a future we can
            # close to avoid the actual network call.
            with pytest.raises((Exception, BaseException)):
                # Run for a few ms then cancel
                asyncio.run(asyncio.wait_for(coro, timeout=0.01))
        except Exception:
            pass

    deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecation_warnings, "expected at least one DeprecationWarning"
    msg = str(deprecation_warnings[0].message)
    assert "deprecated" in msg.lower()
    assert "refresh_and_persist" in msg or "_internal_refresh_access_token" in msg


def test_production_code_does_not_use_old_name():
    """Static-analysis guard: no module under app/ imports the
    deprecated name. Only refresh_and_persist (the safe wrapper) and
    _internal_refresh_access_token (the new canonical name) are
    acceptable imports."""
    import re
    app_root = Path("app")
    bad_imports: list[str] = []
    pattern = re.compile(r"\bimport\s+refresh_access_token\b|from\s+\S+\s+import\s+refresh_access_token\b")
    for p in app_root.rglob("*.py"):
        src = p.read_text()
        # The aliased re-import in claude_oauth_flow.py itself is fine
        if p.name == "claude_oauth_flow.py":
            continue
        if pattern.search(src):
            bad_imports.append(str(p))
    assert not bad_imports, (
        f"production modules still import the deprecated `refresh_access_token`: {bad_imports}. "
        f"Use `refresh_and_persist` (production) or `_internal_refresh_access_token` (tests)."
    )


def test_burn_test_migrated_to_aliased_internal():
    """The single known caller (the live burn test script) must use the
    new canonical name. The ``as refresh_access_token`` rebind is
    acceptable to keep the rest of the script unchanged."""
    src = Path("scripts/test_claude_oauth_live.py").read_text()
    assert "_internal_refresh_access_token as refresh_access_token" in src
    # And the raw deprecated import is gone
    assert "from app.providers.claude_oauth_flow import refresh_access_token\n" not in src


# ── BUG-012: --skip-destructive flag ───────────────────────────────


def test_skip_destructive_flag_parsed():
    """argparse exposes --skip-destructive on the live burn test entry."""
    src = Path("scripts/test_claude_oauth_live.py").read_text()
    assert "--skip-destructive" in src
    assert "argparse" in src
    assert "action=\"store_true\"" in src or "action='store_true'" in src


def test_main_accepts_skip_destructive_kwarg():
    """The signature change is what makes the flag actually take effect."""
    src = Path("scripts/test_claude_oauth_live.py").read_text()
    assert "async def main(skip_destructive: bool = False)" in src


def test_refresh_and_persist_is_the_destructive_test():
    """refresh_and_persist is the test that consumes the rotated
    refresh token — confirm it's the one in _DESTRUCTIVE_TESTS."""
    src = Path("scripts/test_claude_oauth_live.py").read_text()
    assert '_DESTRUCTIVE_TESTS = {"refresh_and_persist"}' in src


def test_skipped_destructive_records_pass_not_fail():
    """When skipped, the test logs as pass (skipped). Otherwise the
    weekly job would record a false negative every run."""
    src = Path("scripts/test_claude_oauth_live.py").read_text()
    idx = src.index("_DESTRUCTIVE_TESTS")
    fn = src[idx:idx + 1500]
    assert "_record(name, True," in fn
    assert "skipped" in fn.lower()
