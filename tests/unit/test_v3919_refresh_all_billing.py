"""v3.9.19 — bulk "Refresh Usage Stats" for claude-oauth accounts.

Operator ask: after Anthropic resets the weekly counters early, the
operator wants one button to re-scrape every claude-oauth account and
re-evaluate rotation rules — so accounts that were auto-skipped over
their cap drop back into service without waiting for the next 4-hour
scrape cycle.

Backend: ``POST /api/providers/_refresh-all-anthropic-billing``.

These tests drive the endpoint coroutine directly with a mocked DB
session + mocked ``scrape_provider_into_snapshot`` (the per-provider
scrape is already covered by test_v370_anthropic_billing.py), plus
source-level wiring checks for the API client method and UI button.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeProvider:
    def __init__(self, pid: str, name: str):
        self.id = pid
        self.name = name


def _mock_db(providers: list[_FakeProvider]) -> MagicMock:
    """An AsyncSession surrogate: db.execute(...) → .scalars().all()."""
    scalars_result = MagicMock()
    scalars_result.all.return_value = providers
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    db = MagicMock()
    db.execute = AsyncMock(return_value=execute_result)
    return db


async def _call(db: MagicMock) -> dict:
    from app.api.anthropic_billing import refresh_all_billing
    return await refresh_all_billing(db=db, _=None)


# ── aggregation logic ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_to_service_counted_from_skip_cleared():
    """A provider whose rotation decision is ``skip_cleared`` (usage
    dropped below threshold) counts as returned_to_service."""
    providers = [_FakeProvider("p1", "VG"), _FakeProvider("p2", "Gmail")]
    db = _mock_db(providers)

    scrape_returns = {
        "p1": {"ok": True, "auth_state": "ok", "seven_day_utilization": 12.0,
               "five_hour_utilization": 3.0,
               "rotation_decision": {"decision": "skip_cleared"}},
        "p2": {"ok": True, "auth_state": "ok", "seven_day_utilization": 88.0,
               "five_hour_utilization": 40.0,
               "rotation_decision": {"decision": "no_change"}},
    }

    async def fake_scrape(_db, provider):
        return scrape_returns[provider.id]

    with patch("app.providers.anthropic_billing.scrape_provider_into_snapshot",
               side_effect=fake_scrape):
        out = await _call(db)

    assert out["providers"] == 2
    assert out["scraped_ok"] == 2
    assert out["returned_to_service"] == 1
    by_id = {r["provider_id"]: r for r in out["results"]}
    assert by_id["p1"]["returned_to_service"] is True
    assert by_id["p2"]["returned_to_service"] is False


@pytest.mark.asyncio
async def test_one_bad_provider_does_not_abort_sweep():
    """If a provider's scrape raises, the sweep records the failure and
    continues to the next provider."""
    providers = [_FakeProvider("p1", "VG"), _FakeProvider("p2", "Gmail")]
    db = _mock_db(providers)

    async def fake_scrape(_db, provider):
        if provider.id == "p1":
            raise RuntimeError("boom")
        return {"ok": True, "auth_state": "ok", "seven_day_utilization": 5.0,
                "rotation_decision": {"decision": "skip_cleared"}}

    with patch("app.providers.anthropic_billing.scrape_provider_into_snapshot",
               side_effect=fake_scrape):
        out = await _call(db)

    assert out["providers"] == 2
    assert out["scraped_ok"] == 1          # only p2 succeeded
    assert out["returned_to_service"] == 1  # p2 cleared
    by_id = {r["provider_id"]: r for r in out["results"]}
    assert by_id["p1"]["ok"] is False
    assert "boom" in by_id["p1"]["error"]


@pytest.mark.asyncio
async def test_failed_scrape_not_counted_as_ok():
    """A scrape that returns ok=False (e.g. expired cookies) is not
    counted in scraped_ok and cannot be returned_to_service."""
    providers = [_FakeProvider("p1", "VG")]
    db = _mock_db(providers)

    async def fake_scrape(_db, provider):
        return {"ok": False, "auth_state": "session_expired",
                "rotation_decision": {}}

    with patch("app.providers.anthropic_billing.scrape_provider_into_snapshot",
               side_effect=fake_scrape):
        out = await _call(db)

    assert out["providers"] == 1
    assert out["scraped_ok"] == 0
    assert out["returned_to_service"] == 0
    assert out["results"][0]["auth_state"] == "session_expired"


@pytest.mark.asyncio
async def test_no_credentialed_providers_returns_empty():
    """No claude-oauth providers with credentials → zeroed summary, no
    crash, no scrape calls."""
    db = _mock_db([])

    with patch("app.providers.anthropic_billing.scrape_provider_into_snapshot",
               side_effect=AssertionError("should not scrape")) as scrape:
        out = await _call(db)

    assert out == {"providers": 0, "scraped_ok": 0,
                   "returned_to_service": 0, "results": []}
    scrape.assert_not_called()


@pytest.mark.asyncio
async def test_missing_rotation_decision_is_safe():
    """A scrape result with no rotation_decision key (or None) must not
    crash the aggregation — returned_to_service is simply False."""
    providers = [_FakeProvider("p1", "VG")]
    db = _mock_db(providers)

    async def fake_scrape(_db, provider):
        return {"ok": True, "auth_state": "ok"}  # no rotation_decision key

    with patch("app.providers.anthropic_billing.scrape_provider_into_snapshot",
               side_effect=fake_scrape):
        out = await _call(db)

    assert out["scraped_ok"] == 1
    assert out["returned_to_service"] == 0
    assert out["results"][0]["returned_to_service"] is False


# ── source-level wiring ────────────────────────────────────────────


def test_endpoint_defined():
    src = Path("app/api/anthropic_billing.py").read_text()
    assert '"/_refresh-all-anthropic-billing"' in src
    assert "async def refresh_all_billing(" in src


def test_endpoint_filters_to_credentialed_claude_oauth():
    """The bulk sweep must only touch claude-oauth providers that have
    credentials configured — same filter the worker uses."""
    src = Path("app/api/anthropic_billing.py").read_text()
    idx = src.index("async def refresh_all_billing(")
    fn = src[idx:idx + 2500]
    assert 'provider_type == "claude-oauth"' in fn
    assert "anthropic_org_uuid.is_not(None)" in fn
    assert "anthropic_session_cookies.is_not(None)" in fn
    assert "deleted_at.is_(None)" in fn


def test_api_client_has_refresh_all_method():
    src = Path("frontend/src/api/index.ts").read_text()
    assert "refreshAllBilling" in src
    assert "/_refresh-all-anthropic-billing" in src


def test_providers_page_has_refresh_button():
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "refreshBillingMutation" in src
    assert "refreshAllBilling" in src
    assert "Refresh Usage Stats" in src
    # v5.3.5 — gate generalized from hasClaudeOauth to
    # hasAnySubscriptionProvider so the button appears on the
    # compliance-locked cluster (where claude-oauth is intentionally
    # tombstoned but ChatGPT-oauth-plan + cursor-oauth are present).
    assert "hasAnySubscriptionProvider" in src
    # Refreshed data invalidates the providers + snapshots queries
    assert "claude-oauth-snapshots" in src
    # v5.3.5 — bulk button fans out to all 3 subscription vendors.
    assert "refreshAllCodexBilling" in src
    assert "refreshAllCursorBilling" in src
