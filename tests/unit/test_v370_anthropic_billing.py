"""v3.7.0 — Anthropic Console billing scraper tests.

Covers parse_cookie_jar, validate_cookies, parse_usage_response, and
the fetch_usage error-classification paths (mocked httpx).
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.providers.anthropic_billing import (
    parse_cookie_jar,
    validate_cookies,
    parse_usage_response,
    fetch_usage,
    ScrapeResult,
)


# ── parse_cookie_jar ───────────────────────────────────────────────


def test_parse_cookies_from_dict():
    out = parse_cookie_jar({"sessionKey": "abc123", "cf_clearance": "xyz"})
    assert out == {"sessionKey": "abc123", "cf_clearance": "xyz"}


def test_parse_cookies_from_json_string():
    raw = '{"sessionKey": "abc", "lastActiveOrg": "uuid-123"}'
    out = parse_cookie_jar(raw)
    assert out["sessionKey"] == "abc"
    assert out["lastActiveOrg"] == "uuid-123"


def test_parse_cookies_from_header_string():
    raw = "sessionKey=abc123; sessionKeyLC=def456; cf_clearance=ghi"
    out = parse_cookie_jar(raw)
    assert out == {"sessionKey": "abc123", "sessionKeyLC": "def456", "cf_clearance": "ghi"}


def test_parse_cookies_strips_quotes():
    raw = 'sessionKey="abc"; lastActiveOrg="uuid"'
    out = parse_cookie_jar(raw)
    assert out == {"sessionKey": "abc", "lastActiveOrg": "uuid"}


def test_parse_cookies_skips_empty_chunks():
    raw = "sessionKey=abc;;  ; lastActiveOrg=uuid"
    out = parse_cookie_jar(raw)
    assert out == {"sessionKey": "abc", "lastActiveOrg": "uuid"}


def test_parse_cookies_rejects_empty():
    with pytest.raises(ValueError):
        parse_cookie_jar("")
    with pytest.raises(ValueError):
        parse_cookie_jar("   ")


def test_parse_cookies_rejects_garbage_string():
    """No '=' anywhere → no cookies → error."""
    with pytest.raises(ValueError):
        parse_cookie_jar("just some text without any equals signs")


def test_parse_cookies_rejects_malformed_json():
    with pytest.raises(ValueError):
        parse_cookie_jar('{"unterminated')


def test_parse_cookies_rejects_non_object_json():
    with pytest.raises(ValueError):
        parse_cookie_jar("[1, 2, 3]")


# ── validate_cookies ───────────────────────────────────────────────


def test_validate_accepts_complete_set():
    """All required + recommended → ok."""
    cookies = {"sessionKey": "abc", "sessionKeyLC": "def", "lastActiveOrg": "uuid"}
    assert validate_cookies(cookies) is None


def test_validate_accepts_minimum_required():
    """Just sessionKey is enough to attempt — others are recommended."""
    assert validate_cookies({"sessionKey": "abc"}) is None


def test_validate_rejects_empty():
    msg = validate_cookies({})
    assert msg is not None and "no cookies" in msg


def test_validate_rejects_missing_session_key():
    msg = validate_cookies({"cf_clearance": "xyz", "lastActiveOrg": "uuid"})
    assert msg is not None and "sessionKey" in msg


# ── parse_usage_response ───────────────────────────────────────────


def _sample_response() -> dict:
    """The response shape captured 2026-05-10 from the VG account."""
    return {
        "five_hour": {
            "utilization": 35.5,
            "resets_at": "2026-05-10T18:00:00+00:00",
        },
        "seven_day": {
            "utilization": 78.7,
            "resets_at": "2026-05-17T16:00:00+00:00",
        },
        "seven_day_oauth_apps": None,
        "seven_day_opus": None,
        "seven_day_sonnet": {
            "utilization": 12.3,
            "resets_at": "2026-05-17T16:00:00+00:00",
        },
        "seven_day_cowork": None,
        "seven_day_omelette": {
            "utilization": 0.5,
            "resets_at": None,
        },
        "tangelo": None,
        "iguana_necktie": None,
        "omelette_promotional": None,
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": 100.0,
            "used_credits": 23.5,
            "utilization": 23.5,
            "currency": "USD",
        },
    }


def test_parse_extracts_seven_day_window():
    out = parse_usage_response(_sample_response())
    assert out["seven_day_utilization"] == 78.7
    assert isinstance(out["seven_day_resets_at"], datetime)
    assert out["seven_day_resets_at"].year == 2026


def test_parse_extracts_five_hour_window():
    out = parse_usage_response(_sample_response())
    assert out["five_hour_utilization"] == 35.5
    assert isinstance(out["five_hour_resets_at"], datetime)


def test_parse_extracts_per_model_breakdown():
    out = parse_usage_response(_sample_response())
    assert out["seven_day_sonnet_utilization"] == 12.3


def test_parse_handles_null_per_model():
    """seven_day_opus is null when account hasn't used opus this period."""
    out = parse_usage_response(_sample_response())
    assert out["seven_day_opus_utilization"] is None
    assert out["seven_day_opus_resets_at"] is None


def test_parse_extracts_extra_usage_block():
    out = parse_usage_response(_sample_response())
    assert out["extra_usage_is_enabled"] is True
    assert out["extra_usage_monthly_limit"] == 100.0
    assert out["extra_usage_used_credits"] == 23.5
    assert out["extra_usage_currency"] == "USD"


def test_parse_handles_unexpected_shapes():
    """Defensive: garbage in → empty out, no raise."""
    assert parse_usage_response({}) == {
        "five_hour_utilization": None, "five_hour_resets_at": None,
        "seven_day_utilization": None, "seven_day_resets_at": None,
        "seven_day_sonnet_utilization": None, "seven_day_sonnet_resets_at": None,
        "seven_day_opus_utilization": None, "seven_day_opus_resets_at": None,
    }


def test_parse_handles_non_dict_input():
    assert parse_usage_response(None) == {}  # type: ignore[arg-type]
    assert parse_usage_response("string") == {}  # type: ignore[arg-type]
    assert parse_usage_response([]) == {}  # type: ignore[arg-type]


def test_parse_handles_string_utilization():
    """If Anthropic ever returns utilization as a string, skip it."""
    body = {"seven_day": {"utilization": "78.7%", "resets_at": "2026-05-17T16:00:00+00:00"}}
    out = parse_usage_response(body)
    assert out["seven_day_utilization"] is None  # rejected — wrong type
    assert out["seven_day_resets_at"] is not None  # but resets_at still parsed


def test_parse_handles_invalid_iso_timestamp():
    body = {"seven_day": {"utilization": 50.0, "resets_at": "not-a-date"}}
    out = parse_usage_response(body)
    assert out["seven_day_utilization"] == 50.0
    assert out["seven_day_resets_at"] is None


# ── fetch_usage error classification (mocked httpx) ────────────────


def _mock_response(*, status: int, body: str = "", json_body: dict | None = None,
                   headers: dict | None = None):
    """Build a mock httpx.Response surrogate for AsyncClient.get."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    resp.headers = headers or {}
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
    else:
        resp.json = MagicMock(side_effect=ValueError("not json"))
    return resp


@pytest.mark.asyncio
async def test_fetch_handles_missing_org_uuid():
    out = await fetch_usage(org_uuid="", cookies={"sessionKey": "abc"})
    assert out.ok is False
    assert out.auth_state == "config_error"
    assert "org_uuid" in out.error


@pytest.mark.asyncio
async def test_fetch_handles_missing_cookies():
    out = await fetch_usage(org_uuid="some-uuid", cookies={})
    assert out.ok is False
    assert out.auth_state == "config_error"


@pytest.mark.asyncio
async def test_fetch_classifies_403_as_session_expired():
    fake = _mock_response(status=403, body='{"error":"unauthorized"}')
    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=fake)
        out = await fetch_usage(org_uuid="some-uuid", cookies={"sessionKey": "x"})
    assert out.ok is False
    assert out.auth_state == "session_expired"
    assert out.http_status == 403


@pytest.mark.asyncio
async def test_fetch_classifies_401_as_session_expired():
    fake = _mock_response(status=401)
    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=fake)
        out = await fetch_usage(org_uuid="some-uuid", cookies={"sessionKey": "x"})
    assert out.ok is False
    assert out.auth_state == "session_expired"


@pytest.mark.asyncio
async def test_fetch_classifies_cloudflare_block():
    """403 + HTML body → cf_blocked, not session_expired."""
    cf_html = "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    fake = _mock_response(status=403, body=cf_html, headers={"cf-mitigated": "challenge"})
    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=fake)
        out = await fetch_usage(org_uuid="some-uuid", cookies={"sessionKey": "x"})
    assert out.ok is False
    assert out.auth_state == "cf_blocked"


@pytest.mark.asyncio
async def test_fetch_classifies_network_error():
    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(side_effect=httpx.ConnectError("network down"))
        out = await fetch_usage(org_uuid="some-uuid", cookies={"sessionKey": "x"})
    assert out.ok is False
    assert out.auth_state == "network_error"


@pytest.mark.asyncio
async def test_fetch_returns_parsed_json_on_200():
    body = _sample_response()
    fake = _mock_response(status=200, body="{...}", json_body=body)
    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=fake)
        out = await fetch_usage(org_uuid="some-uuid", cookies={"sessionKey": "x"})
    assert out.ok is True
    assert out.auth_state == "ok"
    assert out.parsed["seven_day"]["utilization"] == 78.7


@pytest.mark.asyncio
async def test_fetch_classifies_parse_error():
    """200 OK with non-JSON body."""
    fake = _mock_response(status=200, body="<html>not json</html>")
    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=fake)
        out = await fetch_usage(org_uuid="some-uuid", cookies={"sessionKey": "x"})
    assert out.ok is False
    assert out.auth_state == "parse_error"


# ── worker plumbing regression ─────────────────────────────────────


def test_worker_module_loads_and_has_start():
    from app.monitoring import anthropic_billing_worker
    assert hasattr(anthropic_billing_worker, "start")
    assert callable(anthropic_billing_worker.start)


def test_main_lifespan_starts_billing_worker():
    """The worker must be wired into the FastAPI startup hook.

    Source-file read instead of module import — main.py pulls heavy
    runtime deps (structlog, ssl, etc.) that aren't all in the unit-test
    env. We just need to confirm the wiring is present in source.
    """
    from pathlib import Path
    src = Path("app/main.py").read_text()
    assert "anthropic_billing_worker" in src
    assert "_ab_worker.start()" in src


def test_admin_router_is_registered():
    """The /api/providers/{id}/anthropic-billing-* endpoints must be wired."""
    from pathlib import Path
    src = Path("app/main.py").read_text()
    assert "anthropic_billing_router" in src
    assert "include_router(anthropic_billing_router)" in src


def test_settings_has_interval_field():
    from app.config import settings
    assert hasattr(settings, "anthropic_billing_scrape_interval_sec")
    assert settings.anthropic_billing_scrape_interval_sec > 0


def test_provider_model_has_billing_columns():
    """Schema regression — three new columns on Provider."""
    from app.models.db import Provider
    cols = {c.name for c in Provider.__table__.columns}
    assert "anthropic_org_uuid" in cols
    assert "anthropic_session_cookies" in cols
    assert "anthropic_session_captured_at" in cols


def test_external_usage_snapshot_model_exists():
    from app.models.db import ExternalUsageSnapshot
    cols = {c.name for c in ExternalUsageSnapshot.__table__.columns}
    expected = {
        "id", "provider_id", "captured_at", "source", "http_status",
        "error", "auth_state", "five_hour_utilization", "five_hour_resets_at",
        "seven_day_utilization", "seven_day_resets_at",
        "seven_day_sonnet_utilization", "seven_day_opus_utilization",
        "extra_usage_is_enabled", "extra_usage_monthly_limit",
        "extra_usage_used_credits", "extra_usage_utilization",
        "extra_usage_currency", "raw_response",
    }
    missing = expected - cols
    assert not missing, f"missing columns on ExternalUsageSnapshot: {missing}"
