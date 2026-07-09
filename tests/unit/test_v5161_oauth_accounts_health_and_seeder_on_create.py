"""v5.16.1 (#508 P0-2 + P1-5) — seeder-on-create fix + /health.oauthAccounts
observability block.

Found while Playwright-scouting v5.15.2: newly-created OAuth providers
had 0 rows in provider_oauth_accounts because the boot-time seeder had
already fired. Dispatch silently fell back to legacy Provider.api_key
for post-boot providers — technically fine (fallback works) but the
per-account fan-out never engaged for them.

Fix: seeder runs again inside the provider CREATE handler for OAuth-
flavored types. Plus /health gets a new ``oauthAccounts`` block so
operators can see how many providers are single vs multi-account.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── (1) Seeder-on-create wiring ──────────────────────────────────────


def test_create_provider_calls_seeder_for_oauth_types():
    """Static-grep on providers.py CREATE endpoint. If the seeder call
    site is removed or wrapped in a stricter conditional, tests fail
    before merge."""
    src = Path("app/api/providers.py").read_text()
    # Import statement present
    assert "from app.providers.oauth_account_seeder import seed_missing_accounts" in src
    # Called after the create commit
    assert "await seed_missing_accounts(db)" in src
    # Guarded to only OAuth types
    assert '"cursor-oauth"' in src
    assert '"ChatGPT-oauth-plan"' in src  # legacy alias for codex-oauth
    assert '"claude-oauth"' in src


# ── (2) /health.oauthAccounts block wiring ────────────────────────────


def test_health_handler_includes_oauth_accounts_block():
    src = Path("app/api/cluster.py").read_text()
    assert '"oauthAccounts":' in src
    assert "_oauth_accounts_snapshot" in src


def test_oauth_accounts_snapshot_owns_its_session():
    """/health must NOT depend on request-scoped get_db — the block owns
    its own AsyncSessionLocal so /health survives regardless of caller
    session state."""
    src = Path("app/api/cluster.py").read_text()
    # The snapshot helper opens its own session
    assert "AsyncSessionLocal" in src
    assert "async def _oauth_accounts_snapshot" in src


# ── (3) Snapshot shape ────────────────────────────────────────────────


def test_snapshot_module_importable():
    from app.api._oauth_accounts_health import (
        snapshot_oauth_accounts, reset_cache_for_tests, _OAUTH_TYPES,
    )
    assert "cursor-oauth" in _OAUTH_TYPES
    assert "codex-oauth" in _OAUTH_TYPES
    assert "claude-oauth" in _OAUTH_TYPES
    # ChatGPT-oauth-plan is the internal-name for codex-oauth
    assert "ChatGPT-oauth-plan" in _OAUTH_TYPES


def test_snapshot_empty_db_returns_zeros():
    """With no OAuth providers, snapshot has the shape but all zeros —
    doesn't error, doesn't return partial keys."""
    from app.api._oauth_accounts_health import snapshot_oauth_accounts, reset_cache_for_tests

    reset_cache_for_tests()

    # Mock db.execute to return empty results
    mock_db = MagicMock()

    async def _execute(*args, **kwargs):
        result = MagicMock()
        result.all.return_value = []
        result.scalar.return_value = 0
        return result

    mock_db.execute = AsyncMock(side_effect=_execute)

    body = asyncio.run(snapshot_oauth_accounts(mock_db))
    assert body["totalProviders"] == 0
    assert body["totalAccounts"] == 0
    assert body["providersWithMultiple"] == 0
    assert body["providersWithSingle"] == 0
    assert body["providersWithZero"] == 0
    assert body["rotationsLastHour"] == 0
    # by-type map has all three types with zero defaults
    for t in ("cursor-oauth", "codex-oauth", "claude-oauth"):
        assert body["byProviderType"][t] == {"providers": 0, "accounts": 0, "rotations_1h": 0}
    reset_cache_for_tests()


def test_snapshot_caches_result():
    """Second call within 15s returns the same object (cache hit) —
    protects /health from hitting the DB on every request."""
    from app.api._oauth_accounts_health import snapshot_oauth_accounts, reset_cache_for_tests

    reset_cache_for_tests()

    call_count = 0
    mock_db = MagicMock()

    async def _execute(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        result.all.return_value = []
        result.scalar.return_value = 0
        return result

    mock_db.execute = AsyncMock(side_effect=_execute)

    body1 = asyncio.run(snapshot_oauth_accounts(mock_db))
    calls_after_first = call_count
    body2 = asyncio.run(snapshot_oauth_accounts(mock_db))
    calls_after_second = call_count

    assert body1 is body2  # same object (from cache)
    assert calls_after_second == calls_after_first  # no additional DB calls
    reset_cache_for_tests()


def test_snapshot_normalizes_chatgpt_alias_to_codex():
    """Internal ``ChatGPT-oauth-plan`` type is exposed as ``codex-oauth`` in
    the health surface (v3.8.0 rename shim)."""
    from app.api._oauth_accounts_health import snapshot_oauth_accounts, reset_cache_for_tests
    reset_cache_for_tests()

    call_seq = []
    mock_db = MagicMock()

    async def _execute(*args, **kwargs):
        result = MagicMock()
        # Provider count returns ChatGPT-oauth-plan alias
        if not call_seq:
            result.all.return_value = [("ChatGPT-oauth-plan", 1)]
        # Account count same
        elif len(call_seq) == 1:
            result.all.return_value = [("ChatGPT-oauth-plan", 3)]
        # Rotation count
        elif len(call_seq) == 2:
            result.all.return_value = [("ChatGPT-oauth-plan", 2)]
        # Distribution
        elif len(call_seq) == 3:
            result.all.return_value = [("prov-id-1",)]
        else:
            result.all.return_value = []
        result.scalar.return_value = 3
        call_seq.append(1)
        return result

    mock_db.execute = AsyncMock(side_effect=_execute)

    body = asyncio.run(snapshot_oauth_accounts(mock_db))
    # Alias got surfaced as codex-oauth
    assert body["byProviderType"]["codex-oauth"]["providers"] == 1
    assert body["byProviderType"]["codex-oauth"]["accounts"] == 3
    assert body["byProviderType"]["codex-oauth"]["rotations_1h"] == 2
    # cursor + claude both zero
    assert body["byProviderType"]["cursor-oauth"]["providers"] == 0
    assert body["byProviderType"]["claude-oauth"]["providers"] == 0
    reset_cache_for_tests()


# ── (4) Version bumped ───────────────────────────────────────────────


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 16, 1), (
        f"expected >= 5.16.1, got {major}.{minor}.{patch}"
    )
