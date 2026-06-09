"""v5.3.5 — Cursor billing API parity tests.

v4.4.41 shipped the cursor billing scrape worker but no manual-trigger
admin endpoints. This batch adds:

- POST /api/providers/{id}/cursor-billing-refresh (single)
- POST /api/providers/_refresh-all-cursor-billing (bulk)
- POST /api/providers/_refresh-all-codex-billing  (bulk; was missing for Codex too)

Plus frontend pin checks: CursorBillingPanel exists, ProvidersPage
fans out to all 3 vendors, refreshOneUsageMutation routes by type.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Backend source pins ──────────────────────────────────────────────


def test_cursor_billing_module_exists():
    src = Path("app/api/cursor_billing.py").read_text()
    assert "@router.post(\"/{provider_id}/cursor-billing-refresh\")" in src
    assert "@router.post(\"/_refresh-all-cursor-billing\")" in src
    assert "scrape_provider_into_snapshot" in src
    # Enforces the cursor-oauth provider_type guard
    assert 'provider.provider_type != "cursor-oauth"' in src


def test_codex_bulk_endpoint_added():
    """v5.3.5 — parity gap closed. Codex had per-provider refresh but
    no bulk; Anthropic had both. Now all three vendors expose both."""
    src = Path("app/api/codex_billing.py").read_text()
    assert "@router.post(\"/_refresh-all-codex-billing\")" in src
    assert "def refresh_all_codex_billing" in src


def test_main_py_registers_cursor_router():
    src = Path("app/main.py").read_text()
    assert "from app.api.cursor_billing import router as cursor_billing_router" in src
    assert "app.include_router(cursor_billing_router)" in src


# ── Frontend source pins ─────────────────────────────────────────────


def test_cursor_billing_panel_component_exists():
    src = Path("frontend/src/components/providers/CursorBillingPanel.tsx").read_text()
    assert "export function CursorBillingPanel" in src
    assert "refreshCursorBillingNow" in src


def test_provider_form_renders_cursor_panel():
    src = Path("frontend/src/components/providers/ProviderForm.tsx").read_text()
    assert "import { CursorBillingPanel }" in src
    assert "form.provider_type === 'cursor-oauth'" in src


def test_api_client_has_cursor_methods():
    src = Path("frontend/src/api/index.ts").read_text()
    assert "refreshCursorBillingNow:" in src
    assert "refreshAllCursorBilling:" in src
    assert "refreshAllCodexBilling:" in src  # bulk Codex too


def test_providers_page_drops_claude_oauth_gate():
    """Pre-v5.3.5 the bulk button was hidden if hasClaudeOauth=false,
    which silently dropped it on the compliance-locked cluster. v5.3.5
    gates on hasAnySubscriptionProvider instead."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "hasAnySubscriptionProvider" in src
    # Old single-vendor gate should no longer be referenced as the
    # button visibility condition (the variable itself is allowed to
    # exist if used elsewhere, but the .some(...) over all 3 types must
    # be present and is what the JSX consumes).
    assert "p.provider_type === 'cursor-oauth'" in src


def test_providers_page_fans_out_to_all_three_vendors():
    """The bulk button's mutation must hit all 3 endpoints in parallel."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    # All 3 bulk endpoints called via Promise.allSettled
    assert "refreshAllBilling()" in src
    assert "refreshAllCodexBilling()" in src
    assert "refreshAllCursorBilling()" in src
    assert "Promise.allSettled" in src


def test_providers_page_expanded_card_has_per_provider_refresh():
    """v5.3.5 — Refresh Usage button in the expanded card view for the
    3 subscription provider types (claude-oauth / ChatGPT-oauth-plan /
    cursor-oauth)."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    assert "refreshOneUsageMutation" in src
    # Routing by provider_type
    assert "refreshBillingNow(provider.id)" in src
    assert "refreshCodexBillingNow(provider.id)" in src
    assert "refreshCursorBillingNow(provider.id)" in src


# ── Behavioral ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cursor_refresh_endpoint_rejects_non_cursor_provider():
    """The per-provider endpoint must enforce its provider_type guard
    so an operator can't accidentally fire a Cursor scrape against a
    Google or grok-web row."""
    from app.api.cursor_billing import cursor_refresh_now
    from fastapi import HTTPException
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    fake_provider = SimpleNamespace(
        id="prov-not-cursor",
        provider_type="google",
        api_key="some-key",
        deleted_at=None,
    )
    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_rs = MagicMock()
    mock_rs.scalar_one_or_none.return_value = fake_provider
    mock_db.execute.return_value = mock_rs

    with pytest.raises(HTTPException) as exc:
        await cursor_refresh_now("prov-not-cursor", db=mock_db, _=None)
    assert exc.value.status_code == 400
    assert "cursor-oauth" in str(exc.value.detail)
