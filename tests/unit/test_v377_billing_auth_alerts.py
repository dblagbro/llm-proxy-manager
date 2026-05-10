"""v3.7.7 — operator alerts when billing scrape auth fails repeatedly."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.monitoring.notifications import alert_anthropic_billing_auth_expired


# ── alert_anthropic_billing_auth_expired ──────────────────────────


@pytest.mark.asyncio
async def test_alert_calls_send_alert_with_provider_id_and_throttle_key():
    """Throttle key must be per-provider so multiple providers don't
    suppress each other's alerts."""
    with patch("app.monitoring.notifications.send_alert", new=AsyncMock()) as mock:
        await alert_anthropic_billing_auth_expired(
            provider_name="Gmail-Provider", provider_id="abc123",
            auth_state="session_expired", consecutive_failures=2,
        )
    mock.assert_awaited_once()
    kwargs = mock.call_args.kwargs
    assert kwargs["provider_id"] == "abc123"
    assert kwargs["throttle_key"] == "billing_auth:abc123"


@pytest.mark.asyncio
async def test_alert_translates_auth_state_to_human_text():
    """Each auth_state must map to a human-readable explanation in the
    email body."""
    states = [
        ("session_expired", "session cookies have expired"),
        ("cf_blocked", "Cloudflare is challenging"),
        ("config_error", "misconfigured"),
        ("network_error", "network error"),
        ("parse_error", "unparseable response"),
        ("http_error", "unexpected HTTP status"),
    ]
    for state, expected_phrase in states:
        with patch("app.monitoring.notifications.send_alert", new=AsyncMock()) as mock:
            await alert_anthropic_billing_auth_expired(
                provider_name="X", provider_id="x",
                auth_state=state, consecutive_failures=2,
            )
        # The message is positional arg 2 (after severity, subject)
        msg = mock.call_args.args[2] if len(mock.call_args.args) > 2 else mock.call_args.kwargs.get("message", "")
        # Or it could be passed via positional — check both
        if not msg:
            msg = mock.call_args.args[2]
        assert expected_phrase in msg, f"auth_state={state!r} did not translate to expected text"


@pytest.mark.asyncio
async def test_alert_includes_re_capture_instructions():
    """Operator must see WHERE to re-paste cookies (the new UI panel)."""
    with patch("app.monitoring.notifications.send_alert", new=AsyncMock()) as mock:
        await alert_anthropic_billing_auth_expired(
            provider_name="X", provider_id="x",
            auth_state="session_expired", consecutive_failures=3,
        )
    msg = mock.call_args.args[2]
    assert "Rotate cookies" in msg
    assert "claude.ai" in msg


@pytest.mark.asyncio
async def test_alert_severity_is_warning():
    """Not 'error' or 'critical' — billing scrape failure degrades
    rotation but doesn't break inference. Warning is appropriate."""
    with patch("app.monitoring.notifications.send_alert", new=AsyncMock()) as mock:
        await alert_anthropic_billing_auth_expired(
            provider_name="X", provider_id="x",
            auth_state="session_expired", consecutive_failures=2,
        )
    severity = mock.call_args.args[0]
    assert severity == "warning"


@pytest.mark.asyncio
async def test_alert_unknown_state_still_sends():
    """Defensive: an unfamiliar auth_state must still produce an alert
    with a generic message (don't silently swallow novel error types)."""
    with patch("app.monitoring.notifications.send_alert", new=AsyncMock()) as mock:
        await alert_anthropic_billing_auth_expired(
            provider_name="X", provider_id="x",
            auth_state="some_new_state", consecutive_failures=2,
        )
    mock.assert_awaited_once()
    msg = mock.call_args.args[2]
    assert "some_new_state" in msg


# ── scraper wiring regression ─────────────────────────────────────


def test_scraper_calls_alert_after_consecutive_failures():
    """Source-level check that scrape_provider_into_snapshot only fires
    the alert after the 2nd consecutive failure, not on the first."""
    from pathlib import Path
    src = Path("app/providers/anthropic_billing.py").read_text()
    assert "alert_anthropic_billing_auth_expired" in src
    assert "consecutive_failures >= 2" in src
    # Must walk recent snapshots to determine consecutive count
    assert "auth_state ==" in src
    # Must catch exceptions so alert failure doesn't break scrape
    assert "alert_failed" in src or "alert_exc" in src


def test_scraper_only_alerts_on_failure_branch():
    """The alert block must be inside the ``if not result.ok`` branch —
    we never alert on successful scrapes."""
    from pathlib import Path
    src = Path("app/providers/anthropic_billing.py").read_text()
    # The alert helper must appear AFTER the "if not result.ok" guard
    not_ok_idx = src.index("if not result.ok:")
    alert_idx = src.index("alert_anthropic_billing_auth_expired")
    assert not_ok_idx < alert_idx
