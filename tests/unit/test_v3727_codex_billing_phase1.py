"""v3.7.27 (#245) — Codex / ChatGPT Plus usage scrape, Phase 1.

Phase 1 builds the infrastructure (model fields, scraper module,
worker, admin endpoints, UI panel) that lets the operator capture
chatgpt.com analytics cookies + endpoint URL when they have one
from DevTools. The scraper stores raw_response in
external_usage_snapshot with source='chatgpt_codex_v1'; field
extraction (Phase 2) lands later once the response shape is
confirmed against a live capture.

These tests verify the infrastructure plumbing.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Schema additions ───────────────────────────────────────────────


def test_provider_model_has_codex_columns():
    from app.models.db import Provider
    cols = {c.name for c in Provider.__table__.columns}
    assert "codex_session_cookies" in cols
    assert "codex_usage_endpoint_url" in cols
    assert "codex_session_captured_at" in cols


def test_migration_adds_codex_columns():
    src = Path("app/models/database.py").read_text()
    assert "ALTER TABLE providers ADD COLUMN codex_session_cookies" in src
    assert "ALTER TABLE providers ADD COLUMN codex_usage_endpoint_url" in src
    assert "ALTER TABLE providers ADD COLUMN codex_session_captured_at" in src


# ── Scraper module ────────────────────────────────────────────────


def test_codex_billing_module_exists():
    import importlib
    mod = importlib.import_module("app.providers.codex_billing")
    for fn in ("parse_cookie_jar", "validate_endpoint_url", "fetch_usage",
               "parse_usage_response", "scrape_provider_into_snapshot"):
        assert hasattr(mod, fn), f"missing {fn} in codex_billing module"


def test_parse_cookie_jar_accepts_json():
    from app.providers.codex_billing import parse_cookie_jar
    out = parse_cookie_jar('{"sessionToken": "abc", "cf_clearance": "xyz"}')
    assert out == {"sessionToken": "abc", "cf_clearance": "xyz"}


def test_parse_cookie_jar_accepts_header_style():
    from app.providers.codex_billing import parse_cookie_jar
    out = parse_cookie_jar("name1=val1; name2=val2")
    assert out == {"name1": "val1", "name2": "val2"}


def test_parse_cookie_jar_rejects_empty():
    from app.providers.codex_billing import parse_cookie_jar
    with pytest.raises(ValueError):
        parse_cookie_jar("")
    with pytest.raises(ValueError):
        parse_cookie_jar("   ")


def test_validate_endpoint_url_requires_https():
    from app.providers.codex_billing import validate_endpoint_url
    assert validate_endpoint_url("http://chatgpt.com/foo") is not None
    assert "HTTPS" in validate_endpoint_url("http://chatgpt.com/foo")
    assert validate_endpoint_url("") is not None
    assert validate_endpoint_url(None) is not None


def test_validate_endpoint_url_requires_known_host():
    from app.providers.codex_billing import validate_endpoint_url
    assert validate_endpoint_url("https://example.com/foo") is not None
    assert validate_endpoint_url("https://chatgpt.com/api/usage") is None
    assert validate_endpoint_url("https://chat.openai.com/foo") is None


def test_parse_usage_response_is_phase1_stub():
    """Phase 1 returns empty dict — field extraction is Phase 2."""
    from app.providers.codex_billing import parse_usage_response
    assert parse_usage_response({"any": "shape"}) == {}
    assert parse_usage_response(None) == {}


# ── Worker module ─────────────────────────────────────────────────


def test_worker_module_exists():
    import importlib
    mod = importlib.import_module("app.monitoring.codex_billing_worker")
    for fn in ("start", "_interval_sec", "_freshness_floor_sec",
               "_latest_snapshot_age_sec", "_scrape_all_once", "_scrape_loop"):
        assert hasattr(mod, fn), f"missing {fn} in codex_billing_worker"


def test_worker_uses_freshness_guard_pattern():
    """Same v3.7.24 dedup pattern as the Anthropic worker — MAX subquery
    + interval/2 default + random startup jitter."""
    src = Path("app/monitoring/codex_billing_worker.py").read_text()
    assert "func.max(ExternalUsageSnapshot.captured_at)" in src
    assert 'ExternalUsageSnapshot.source == "chatgpt_codex_v1"' in src
    assert "interval_sec // 2" in src
    assert "random.uniform" in src


def test_worker_filters_to_codex_oauth():
    """Worker only scrapes codex-oauth providers with credentials set."""
    src = Path("app/monitoring/codex_billing_worker.py").read_text()
    assert 'Provider.provider_type == "ChatGPT-oauth-plan"' in src
    assert "Provider.codex_usage_endpoint_url.is_not(None)" in src
    assert "Provider.codex_session_cookies.is_not(None)" in src


def test_config_has_codex_billing_settings():
    from app.config import settings
    assert hasattr(settings, "codex_billing_scrape_interval_sec")
    assert hasattr(settings, "codex_billing_min_scrape_gap_sec")
    # Default 4h
    assert settings.codex_billing_scrape_interval_sec == 14400


# ── Admin endpoints ───────────────────────────────────────────────


def test_codex_billing_api_module_exists():
    import importlib
    mod = importlib.import_module("app.api.codex_billing")
    assert hasattr(mod, "router")


def test_endpoints_registered_in_router():
    from app.api.codex_billing import router
    paths = {r.path for r in router.routes}
    assert "/api/providers/{provider_id}/codex-billing-credentials" in paths
    assert "/api/providers/{provider_id}/codex-billing-refresh" in paths


def test_credentials_endpoint_gated_on_provider_type():
    """The POST credentials handler must reject non-codex-oauth provider
    types — the cookies wouldn't be usable on a claude-oauth provider."""
    src = Path("app/api/codex_billing.py").read_text()
    assert 'provider_type != "ChatGPT-oauth-plan"' in src


def test_worker_wired_into_main_lifespan():
    src = Path("app/main.py").read_text()
    assert "codex_billing_worker" in src
    assert "_cb_worker.start()" in src


def test_router_included_in_app():
    src = Path("app/main.py").read_text()
    assert "from app.api.codex_billing import router as codex_billing_router" in src
    assert "app.include_router(codex_billing_router)" in src


# ── Provider serializer ─────────────────────────────────────────


def test_provider_serializer_surfaces_codex_fields():
    """The to_dict-style serializer in app/api/providers.py must
    expose has_codex_session_cookies + codex_usage_endpoint_url +
    codex_session_captured_at so the UI can render the panel state.
    The raw cookies value must NOT be surfaced."""
    src = Path("app/api/providers.py").read_text()
    assert '"codex_usage_endpoint_url"' in src
    assert '"has_codex_session_cookies"' in src
    assert '"codex_session_captured_at"' in src
    # Raw cookies must never appear in the response
    assert '"codex_session_cookies":' not in src


# ── Scrape lifecycle ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scrape_provider_returns_reason_when_no_credentials():
    from app.providers.codex_billing import scrape_provider_into_snapshot
    fake_provider = MagicMock()
    fake_provider.codex_usage_endpoint_url = None
    fake_provider.codex_session_cookies = None
    fake_db = MagicMock()
    result = await scrape_provider_into_snapshot(fake_db, fake_provider)
    assert result["ok"] is False
    assert "no codex billing credentials" in result["reason"]


# ── Frontend wiring ───────────────────────────────────────────────


def test_frontend_provider_type_has_codex_fields():
    src = Path("frontend/src/types/index.ts").read_text()
    assert "codex_usage_endpoint_url" in src
    assert "has_codex_session_cookies" in src
    assert "codex_session_captured_at" in src


def test_frontend_api_exposes_codex_billing_calls():
    src = Path("frontend/src/api/index.ts").read_text()
    assert "setCodexBillingCredentials" in src
    assert "refreshCodexBillingNow" in src
    assert "/codex-billing-credentials" in src
    assert "/codex-billing-refresh" in src


def test_codex_billing_panel_component_exists():
    p = Path("frontend/src/components/providers/CodexBillingPanel.tsx")
    assert p.exists(), "CodexBillingPanel.tsx must exist"
    src = p.read_text()
    assert "External Usage (ChatGPT / Codex Cloud)" in src
    # Operator must have BOTH the endpoint URL input and the cookies textarea
    assert "Analytics endpoint URL" in src
    assert "chatgpt.com cookies" in src


def test_provider_form_renders_codex_panel_only_for_codex_oauth():
    src = Path("frontend/src/components/providers/ProviderForm.tsx").read_text()
    assert "CodexBillingPanel" in src
    # Gating expression must match form.provider_type === 'ChatGPT-oauth-plan'
    assert "form.provider_type === 'ChatGPT-oauth-plan'" in src


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 27)
