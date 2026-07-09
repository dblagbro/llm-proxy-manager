"""v5.15.1 Phase 2 (#508) — dispatch flip.

The v5.15.0 Phase 1 seeder ensured every OAuth provider has at least
one child row in ``provider_oauth_accounts``. v5.15.1 wires
``apply_fanout_to_kwargs`` into ``messages.py:_call_with_route`` so
dispatch actually reads from the accounts table (with legacy
``Provider.api_key`` as safe-fallback for providers without accounts).

This test file locks in:
- Selector's ``apply_fanout_to_kwargs`` helper exists + short-circuits
  correctly on non-OAuth providers, disabled fan-out, no accounts
- Wiring is present in messages.py dispatch site
- Version bumped
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── (1) Selector helper surface ─────────────────────────────────────


def test_apply_fanout_helper_importable():
    from app.providers.oauth_account_selector import (
        apply_fanout_to_kwargs, resolve_access_token,
        resolve_strategy, VALID_STRATEGIES, _OAUTH_PROVIDER_TYPES,
    )


def test_valid_strategies_locked():
    from app.providers.oauth_account_selector import VALID_STRATEGIES
    assert VALID_STRATEGIES == {"least_utilized", "round_robin", "least_recently_used"}


def test_oauth_provider_types_locked():
    from app.providers.oauth_account_selector import _OAUTH_PROVIDER_TYPES
    assert _OAUTH_PROVIDER_TYPES == {"cursor-oauth", "codex-oauth", "claude-oauth"}


def test_apply_fanout_no_op_for_non_oauth_provider():
    """A provider whose type isn't OAuth MUST NOT touch kwargs or DB."""
    from app.providers.oauth_account_selector import apply_fanout_to_kwargs
    kwargs = {"api_key": "legacy-token"}
    provider = MagicMock()
    provider.provider_type = "anthropic"
    db = MagicMock()  # unused
    result = asyncio.run(apply_fanout_to_kwargs(kwargs, provider, db))
    assert result is None
    assert kwargs["api_key"] == "legacy-token"  # unchanged


def test_apply_fanout_no_op_when_disabled_globally():
    from app.providers.oauth_account_selector import apply_fanout_to_kwargs
    from app.config import settings as _s
    kwargs = {"api_key": "legacy-token"}
    provider = MagicMock()
    provider.provider_type = "cursor-oauth"
    db = MagicMock()

    orig = _s.oauth_account_fanout_enabled
    _s.oauth_account_fanout_enabled = False
    try:
        result = asyncio.run(apply_fanout_to_kwargs(kwargs, provider, db))
    finally:
        _s.oauth_account_fanout_enabled = orig
    assert result is None
    assert kwargs["api_key"] == "legacy-token"


# ── (2) Wiring into messages.py ─────────────────────────────────────


def test_messages_dispatch_calls_apply_fanout():
    src = Path("app/api/messages.py").read_text()
    assert "from app.providers.oauth_account_selector import apply_fanout_to_kwargs" in src
    assert "apply_fanout_to_kwargs(" in src
    # And emits the observability header when an account was picked.
    assert 'X-OAuth-Account' in src


# ── (3) Strategy resolution ─────────────────────────────────────────


def test_resolve_strategy_uses_provider_override():
    from app.providers.oauth_account_selector import resolve_strategy
    p = MagicMock()
    p.oauth_account_strategy = "round_robin"
    assert resolve_strategy(p) == "round_robin"


def test_resolve_strategy_falls_back_to_settings_default():
    from app.providers.oauth_account_selector import resolve_strategy
    p = MagicMock()
    p.oauth_account_strategy = None
    # Default is "least_utilized" per operator sign-off 2026-06-30
    assert resolve_strategy(p) == "least_utilized"


def test_resolve_strategy_degrades_on_unknown_string():
    """A typo in provider.oauth_account_strategy MUST NOT break dispatch."""
    from app.providers.oauth_account_selector import resolve_strategy
    p = MagicMock()
    p.oauth_account_strategy = "nonsense_typo"
    assert resolve_strategy(p) == "least_utilized"


# ── (4) Version bumped ──────────────────────────────────────────────


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 15, 1), (
        f"expected >= 5.15.1, got {major}.{minor}.{patch}"
    )
