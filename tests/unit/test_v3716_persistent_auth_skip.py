"""v3.7.16 — persistent auth-failure → DB-persisted auto_skip (#239)
plus the config_runtime SCHEMA type harmonization (#238)."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── #238: config_runtime SCHEMA type harmonized ────────────────────


def test_semantic_cache_schema_uses_str_not_string():
    """Old type='string' triggered a warning on every settings load
    because pydantic reports 'str'. Harmonize to 'str'."""
    from app.config_runtime import SCHEMA
    assert SCHEMA["semantic_cache_embedding_model"]["type"] == "str"
    assert SCHEMA["semantic_cache_provider_id"]["type"] == "str"


# ── #239: persistent auth-failure threshold + escalation ──────────


def test_constants_exist():
    from app.routing import circuit_breaker as cb
    assert cb.PERSISTENT_AUTH_THRESHOLD >= 2
    assert cb.PERSISTENT_AUTH_WINDOW_SEC >= 60.0


def test_history_dict_exists():
    from app.routing import circuit_breaker as cb
    assert hasattr(cb, "_auth_failure_history")
    assert isinstance(cb._auth_failure_history, dict)


def test_clear_auth_failure_also_clears_history():
    """clear_auth_failure must reset the per-provider failure counter,
    else a successful re-auth doesn't reset the threshold."""
    from app.routing import circuit_breaker as cb
    cb._auth_failure_history["p1"] = [time.time(), time.time()]
    cb.clear_auth_failure("p1")
    assert "p1" not in cb._auth_failure_history


@pytest.mark.asyncio
async def test_record_auth_failure_appends_history():
    """Each call to record_auth_failure must extend the per-provider
    history list — that's what _persist_auto_skip uses to decide."""
    from app.routing import circuit_breaker as cb
    cb._auth_failure_history.pop("p2", None)
    cb._auth_failed.pop("p2", None)
    # Patch the persistence call so this unit test doesn't need a real DB
    with patch.object(cb, "_persist_auto_skip", new=AsyncMock()):
        await cb.record_auth_failure("p2", "401 invalid_token")
    assert "p2" in cb._auth_failure_history
    assert len(cb._auth_failure_history["p2"]) == 1


@pytest.mark.asyncio
async def test_first_failure_persists():
    """v5.22.20 inverted this deliberately.

    It used to assert that 1-2 failures must NOT escalate, to avoid a blip
    24h-skipping a provider. That protection was illusory:
    ``record_auth_failure`` sets ``hold_down_until = now + 86400``
    unconditionally, so failure #1 already pulls the provider for 24 hours.
    The threshold only decided whether that decision was written down — and
    once the breaker opens, ``is_available()`` is False, so failures #2 and #3
    cannot occur for another day.

    Observed live on Devin-Codex-Gmail: one failure marked, then nothing for
    24h, with ``auto_skip_until`` stale for a month while the provider was
    demonstrably dead. Persisting immediately is not more aggressive than the
    breaker it accompanies; it makes that decision survive a restart.
    """
    from app.routing import circuit_breaker as cb
    cb.clear_auth_failure("p3")
    fake_persist = AsyncMock()
    with patch.object(cb, "_persist_auto_skip", new=fake_persist):
        await cb.record_auth_failure("p3", "auth err 1")
    assert fake_persist.await_count == 1
    assert fake_persist.await_args[0][0] == "p3"
    cb.clear_auth_failure("p3")


@pytest.mark.asyncio
async def test_repeated_failures_each_persist():
    """Every auth failure escalates (v5.22.20); the provider id must be
    passed through each time."""
    from app.routing import circuit_breaker as cb
    cb._auth_failure_history.pop("p4", None)
    cb._auth_failed.pop("p4", None)
    fake_persist = AsyncMock()
    with patch.object(cb, "_persist_auto_skip", new=fake_persist):
        await cb.record_auth_failure("p4", "auth err 1")
        await cb.record_auth_failure("p4", "auth err 2")
        await cb.record_auth_failure("p4", "auth err 3")
    # v5.22.20 — every auth failure escalates now, not just the third.
    assert fake_persist.await_count == 3
    args, kwargs = fake_persist.await_args
    assert args[0] == "p4"


@pytest.mark.asyncio
async def test_window_prunes_old_failures():
    """A failure older than the window must be pruned from the history."""
    from app.routing import circuit_breaker as cb
    cb._auth_failure_history.pop("p5", None)
    cb._auth_failed.pop("p5", None)
    # Seed with two failures that are older than the window
    old_t = time.time() - cb.PERSISTENT_AUTH_WINDOW_SEC - 60
    cb._auth_failure_history["p5"] = [old_t, old_t]
    fake_persist = AsyncMock()
    with patch.object(cb, "_persist_auto_skip", new=fake_persist):
        await cb.record_auth_failure("p5", "fresh failure")
    # Pruning still matters: the history list feeds the
    # ``auth_failure_marked`` log line's consecutive_in_window field, so a
    # stale entry would misreport how bad the failure run actually is.
    # (Escalation no longer depends on it — see test_first_failure_persists.)
    assert len(cb._auth_failure_history["p5"]) == 1


@pytest.mark.asyncio
async def test_persist_auto_skip_writes_db_fields():
    """_persist_auto_skip must update Provider.auto_skip_until +
    auto_skip_reason fields."""
    from app.routing import circuit_breaker as cb
    from app.models.db import Provider
    p = Provider(
        id="p6", name="test", provider_type="ChatGPT-oauth-plan",
        api_key="x", enabled=True, priority=1,
        auto_skip_until=None, auto_skip_reason=None,
    )
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    rs = MagicMock()
    rs.scalar_one_or_none = MagicMock(return_value=p)
    db.execute = AsyncMock(return_value=rs)
    db.commit = AsyncMock()
    with patch("app.models.database.AsyncSessionLocal", return_value=db):
        await cb._persist_auto_skip("p6", "auth err")
    assert p.auto_skip_until is not None
    assert p.auto_skip_reason == "persistent_auth_failure"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_auto_skip_idempotent():
    """If provider already has auto_skip_until > now+24h, don't shorten it."""
    from app.routing import circuit_breaker as cb
    from app.models.db import Provider
    from datetime import datetime, timedelta
    far_future = datetime.utcnow() + timedelta(hours=48)
    p = Provider(
        id="p7", name="test", provider_type="ChatGPT-oauth-plan",
        api_key="x", enabled=True, priority=1,
        auto_skip_until=far_future, auto_skip_reason="billing_100pct",
    )
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    rs = MagicMock()
    rs.scalar_one_or_none = MagicMock(return_value=p)
    db.execute = AsyncMock(return_value=rs)
    db.commit = AsyncMock()
    original_until = p.auto_skip_until
    original_reason = p.auto_skip_reason
    with patch("app.models.database.AsyncSessionLocal", return_value=db):
        await cb._persist_auto_skip("p7", "auth err")
    # Should leave existing further-out skip alone
    assert p.auto_skip_until == original_until
    assert p.auto_skip_reason == original_reason
    db.commit.assert_not_called()


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 16)
