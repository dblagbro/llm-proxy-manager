"""v3.7.27 (#245 Phase 1) — ChatGPT/Codex usage scrape scaffolding.

Phase 1 shipped: Provider columns, scraper module, 4h worker, admin
endpoint, frontend panel. Operator workflow ORIGINALLY required pasting
cookies + analytics endpoint URL captured from DevTools.

Phase 2 (v3.8.1) replaced the cookie-paste flow with the existing OAuth
access_token — the dev-tools capture confirmed
``chatgpt.com/backend-api/wham/usage`` accepts the same bearer the
inference path uses. The legacy columns + paste endpoints remain in
the schema/API as no-ops for back-compat.

This test file is reduced to the Phase 1 *infrastructure* invariants
that still hold after Phase 2 (table columns + worker module + router).
The Phase 1 behavior tests for the cookie-paste path were superseded
by ``test_v381_codex_billing_phase2.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Provider schema (Phase 1 columns retained as no-ops) ───────────


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


# ── Module + router structure (still present in Phase 2) ──────────


def test_codex_billing_module_exists():
    import importlib
    mod = importlib.import_module("app.providers.codex_billing")
    for fn in ("fetch_usage", "parse_usage_response", "scrape_provider_into_snapshot"):
        assert hasattr(mod, fn), f"missing {fn} in codex_billing module"


def test_worker_module_exists():
    import importlib
    mod = importlib.import_module("app.monitoring.codex_billing_worker")
    for fn in ("start", "_interval_sec", "_freshness_floor_sec",
               "_latest_snapshot_age_sec", "_scrape_all_once", "_scrape_loop"):
        assert hasattr(mod, fn), f"missing {fn} in codex_billing_worker"


def test_codex_billing_api_module_exists():
    import importlib
    mod = importlib.import_module("app.api.codex_billing")
    assert hasattr(mod, "router")


def test_router_included_in_app():
    src = Path("app/main.py").read_text()
    assert "from app.api.codex_billing import router as codex_billing_router" in src
    assert "app.include_router(codex_billing_router)" in src


def test_worker_wired_into_main_lifespan():
    src = Path("app/main.py").read_text()
    assert "codex_billing_worker" in src
    assert "_cb_worker.start()" in src


def test_refresh_endpoint_still_registered():
    """The manual-trigger endpoint stays — it's how operator smoke-tests
    after enabling a provider."""
    from app.api.codex_billing import router
    paths = {r.path for r in router.routes}
    assert "/api/providers/{provider_id}/codex-billing-refresh" in paths


def test_config_has_codex_billing_settings():
    from app.config import settings
    assert hasattr(settings, "codex_billing_scrape_interval_sec")
    assert hasattr(settings, "codex_billing_min_scrape_gap_sec")
    assert settings.codex_billing_scrape_interval_sec == 14400
