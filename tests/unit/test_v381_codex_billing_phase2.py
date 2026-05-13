"""v3.8.1 (#245 Phase 2) — ChatGPT/Codex usage scrape using OAuth bearer.

Phase 2 replaces the cookie-paste flow from Phase 1 with the existing
OAuth access_token (Provider.api_key) that the codex-oauth refresh
flow already maintains. Endpoint hardcoded to
``https://chatgpt.com/backend-api/wham/usage`` (discovered via the
2026-05-13 dev-tools capture; bearer-only auth confirmed via live test).

These tests verify the new behavior. Phase 1 cookie-paste assertions
removed.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Endpoint + auth posture ────────────────────────────────────────


def test_endpoint_url_hardcoded():
    from app.providers.codex_billing import USAGE_ENDPOINT
    assert USAGE_ENDPOINT == "https://chatgpt.com/backend-api/wham/usage"


def test_fetch_uses_bearer_auth_not_cookies():
    src = Path("app/providers/codex_billing.py").read_text()
    # Bearer auth pattern
    assert 'f"Bearer {access_token}"' in src
    # No cookie-based fetch path anymore (was the Phase 1 design)
    assert "cookies=cookies" not in src


def test_fetch_does_not_require_oai_headers():
    """The 2026-05-13 live test confirmed bearer-only suffices —
    OAI-Session-Id / X-OAI-IS / OAI-Device-Id are NOT needed for the
    /wham/usage endpoint. Verify they're absent from the actual
    request headers dict (narrow the search to the function body so
    documentation comments mentioning these headers don't trigger
    the check)."""
    src = Path("app/providers/codex_billing.py").read_text()
    # Locate the headers dict inside _fetch_with_token
    fn_idx = src.index("async def _fetch_with_token")
    # Find the headers= kwarg that follows
    headers_idx = src.index("headers={", fn_idx)
    # The headers dict ends at the matching }, find it
    end_idx = src.index("},", headers_idx) + 1
    headers_block = src[headers_idx:end_idx]
    for forbidden in ("X-OAI-IS", "OAI-Session-Id", "OAI-Device-Id",
                      "OAI-Client-Build-Number", "OAI-Client-Version"):
        assert forbidden not in headers_block, (
            f"_fetch_with_token's headers dict includes {forbidden}; "
            f"the 2026-05-13 live test confirmed bearer-only suffices."
        )


# ── parse_usage_response — real extraction (not a stub anymore) ───


def test_parse_extracts_five_hour_window():
    from app.providers.codex_billing import parse_usage_response
    body = {
        "rate_limit": {
            "primary_window": {
                "used_percent": 30,
                "limit_window_seconds": 18000,
                "reset_at": 1778725635,
            },
            "secondary_window": {
                "used_percent": 5,
                "limit_window_seconds": 604800,
                "reset_at": 1779312435,
            },
        },
    }
    out = parse_usage_response(body)
    assert out["five_hour_utilization"] == 30.0
    assert out["seven_day_utilization"] == 5.0
    assert isinstance(out["five_hour_resets_at"], datetime)
    assert isinstance(out["seven_day_resets_at"], datetime)


def test_parse_validates_window_duration():
    """The binding to five_hour_* vs seven_day_* checks
    ``limit_window_seconds`` against the expected duration, so a
    swapped/mislabeled response doesn't silently mis-bin the data."""
    from app.providers.codex_billing import parse_usage_response
    body = {
        "rate_limit": {
            # Wrong duration in primary slot
            "primary_window": {
                "used_percent": 99,
                "limit_window_seconds": 999,  # not 5h
                "reset_at": 1778725635,
            },
        },
    }
    out = parse_usage_response(body)
    assert out["five_hour_utilization"] is None
    assert out["five_hour_resets_at"] is None


def test_parse_handles_missing_rate_limit():
    """No rate_limit key in response → all extracted fields None
    (defensive — never raise on shape changes)."""
    from app.providers.codex_billing import parse_usage_response
    out = parse_usage_response({"plan_type": "prolite"})
    assert out["five_hour_utilization"] is None
    assert out["seven_day_utilization"] is None
    assert out["five_hour_resets_at"] is None
    assert out["seven_day_resets_at"] is None


def test_parse_handles_non_dict_input():
    from app.providers.codex_billing import parse_usage_response
    assert parse_usage_response(None) == {}
    assert parse_usage_response("not a dict") == {}
    assert parse_usage_response([1, 2, 3]) == {}


def test_parse_handles_non_numeric_used_percent():
    from app.providers.codex_billing import parse_usage_response
    body = {
        "rate_limit": {
            "primary_window": {
                "used_percent": "thirty",  # bad type from upstream
                "limit_window_seconds": 18000,
                "reset_at": 1778725635,
            },
        },
    }
    out = parse_usage_response(body)
    assert out["five_hour_utilization"] is None


# ── Lazy bearer refresh on 401 ────────────────────────────────────


def test_fetch_attempts_refresh_on_401():
    """When the bearer returns 401 and the provider has a refresh_token,
    fetch_usage must try ONE refresh via codex_oauth_flow then retry."""
    src = Path("app/providers/codex_billing.py").read_text()
    idx = src.index("async def fetch_usage")
    body = src[idx:idx + 4000]
    assert "http_status == 401" in body
    assert "refresh_and_persist" in body
    # Only ONE retry — no infinite loop on persistent 401
    # (the retry result is checked against 401 below but not refreshed again)
    assert "bearer_refresh_attempt" in body


def test_fetch_does_not_loop_on_persistent_401():
    """After the one refresh attempt, a subsequent 401 returns
    session_expired — no retry storm. Count only invocations
    (``refresh_and_persist(``), not the import statement that also
    contains the symbol."""
    src = Path("app/providers/codex_billing.py").read_text()
    fn_idx = src.index("async def fetch_usage")
    next_fn = src.index("async def ", fn_idx + 20)
    body = src[fn_idx:next_fn]
    # Exactly one call site
    assert body.count("refresh_and_persist(") == 1


# ── Worker filter ──────────────────────────────────────────────────


def test_worker_filters_by_api_key_not_cookies():
    """Phase 2 worker selects providers with api_key set (= OAuth
    access_token present), NOT the legacy codex_session_cookies."""
    src = Path("app/monitoring/codex_billing_worker.py").read_text()
    assert "Provider.api_key.is_not(None)" in src
    # The Phase 1 cookie filter should be gone
    assert "Provider.codex_session_cookies.is_not(None)" not in src


# ── API endpoint behavior ─────────────────────────────────────────


def test_credentials_endpoint_is_deprecated_noop():
    """Phase 1's /codex-billing-credentials endpoint is now a no-op
    compatibility shim. It must accept the legacy body shape, return
    200 with a deprecated:true flag, and not error."""
    src = Path("app/api/codex_billing.py").read_text()
    idx = src.index("async def store_codex_credentials_legacy")
    body = src[idx:idx + 2500]
    assert '"deprecated": True' in body
    assert "no longer requires pasted cookies" in body


def test_refresh_endpoint_uses_oauth_token():
    """The manual refresh endpoint must check for OAuth token before
    firing, with a clear error if the provider hasn't been auth'd yet."""
    src = Path("app/api/codex_billing.py").read_text()
    idx = src.index("async def codex_refresh_now")
    body = src[idx:idx + 2500]
    assert "provider.api_key" in body
    assert "OAuth" in body or "oauth" in body


# ── Frontend panel ────────────────────────────────────────────────


def test_panel_does_not_render_paste_form():
    """Phase 2 panel removed the credentials-paste UI."""
    src = Path("frontend/src/components/providers/CodexBillingPanel.tsx").read_text()
    # The textarea + analytics-URL input from Phase 1 should be gone
    assert "Paste credentials" not in src
    assert "<textarea" not in src
    assert "Analytics endpoint URL" not in src


def test_panel_mentions_oauth_token_path():
    src = Path("frontend/src/components/providers/CodexBillingPanel.tsx").read_text()
    assert "OAuth access" in src or "OAuth access_token" in src


def test_panel_still_has_refresh_button():
    """The smoke-test refresh button stays — operator can verify the
    scrape works without waiting for the 4h cadence."""
    src = Path("frontend/src/components/providers/CodexBillingPanel.tsx").read_text()
    assert "Refresh now" in src
    assert "handleRefreshNow" in src


# ── Scrape lifecycle (happy path) ─────────────────────────────────


@pytest.mark.asyncio
async def test_scrape_provider_returns_reason_when_no_oauth_token():
    """A provider with no api_key (= no OAuth access_token) gets a
    clear failure reason rather than crashing."""
    from app.providers.codex_billing import fetch_usage
    fake_provider = MagicMock()
    fake_provider.api_key = None
    result = await fetch_usage(fake_provider, None)
    assert result.ok is False
    assert result.auth_state == "config_error"


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 8, 1)
