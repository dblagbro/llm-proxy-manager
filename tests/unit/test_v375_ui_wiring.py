"""v3.7.5 — UI wiring regression checks (source-level — no React/TS unit tests
in this codebase; we verify the source file structure instead)."""
from __future__ import annotations

from pathlib import Path


def test_anthropic_billing_panel_component_exists():
    """The new component file must exist."""
    p = Path("frontend/src/components/providers/AnthropicBillingPanel.tsx")
    assert p.exists(), f"missing component file: {p}"


def test_provider_form_imports_billing_panel():
    src = Path("frontend/src/components/providers/ProviderForm.tsx").read_text()
    assert "AnthropicBillingPanel" in src
    assert "import { AnthropicBillingPanel }" in src


def test_provider_form_renders_panel_for_claude_oauth():
    """Panel must be conditional on provider_type === 'claude-oauth' AND editing
    (need a stored provider to query snapshots)."""
    src = Path("frontend/src/components/providers/ProviderForm.tsx").read_text()
    assert "form.provider_type === 'claude-oauth'" in src
    assert "<AnthropicBillingPanel" in src


def test_provider_form_takes_provider_prop():
    """The Provider object must be threaded through for the panel to render
    current state (org_uuid / cookies / auto_skip_until / snapshots)."""
    src = Path("frontend/src/components/providers/ProviderForm.tsx").read_text()
    assert "provider?: Provider" in src
    assert "onProviderUpdated?:" in src


def test_old_usage_section_marks_superseded_for_claude_oauth():
    """The legacy 'Usage-based rotation' section must surface a
    'superseded' note when the provider is claude-oauth."""
    src = Path("frontend/src/components/providers/ProviderForm.tsx").read_text()
    assert "superseded by External Usage above" in src


def test_providers_page_passes_provider_to_form():
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    # The Modal must pass the editing object and an updated-callback
    assert "provider={editing ?? undefined}" in src
    assert "onProviderUpdated=" in src


def test_api_client_has_billing_endpoints():
    src = Path("frontend/src/api/index.ts").read_text()
    assert "setBillingCredentials" in src
    assert "refreshBillingNow" in src
    assert "listSnapshots" in src
    assert "evaluateRotationRulesNow" in src
    # Endpoints point at the right paths
    assert "/anthropic-billing-credentials" in src
    assert "/anthropic-billing-refresh" in src
    assert "/external-usage" in src
    assert "/_evaluate-rotation-rules" in src


def test_provider_type_includes_new_fields():
    src = Path("frontend/src/types/index.ts").read_text()
    assert "anthropic_org_uuid" in src
    assert "has_anthropic_session_cookies" in src
    assert "anthropic_session_captured_at" in src
    assert "auto_skip_until" in src
    assert "auto_skip_reason" in src


def test_panel_has_paste_workflow():
    """The panel must have an explicit 'Paste cookies' button + textarea + Save."""
    src = Path("frontend/src/components/providers/AnthropicBillingPanel.tsx").read_text()
    assert "Paste cookies" in src or "Rotate cookies" in src
    assert "Save credentials" in src
    assert "Refresh now" in src
    assert "Cookie blob" in src or "cookie blob" in src.lower()


def test_panel_renders_auto_skip_banner():
    """When auto_skip_until is set, the panel must show a red banner."""
    src = Path("frontend/src/components/providers/AnthropicBillingPanel.tsx").read_text()
    assert "Auto-skipped" in src
    assert "skip_until" in src
    assert "skip_reason" in src


def test_panel_renders_snapshots_table():
    src = Path("frontend/src/components/providers/AnthropicBillingPanel.tsx").read_text()
    assert "Recent snapshots" in src
    assert "snapshots.map" in src
    # Per-model breakdown column
    assert "seven_day_sonnet_utilization" in src


def test_panel_shows_cookie_age():
    """'cookies are N days old' badge + warning when >= 25 days."""
    src = Path("frontend/src/components/providers/AnthropicBillingPanel.tsx").read_text()
    assert "captured" in src.lower()
    assert "refresh soon" in src.lower() or "captured_days" in src


def test_panel_does_not_expose_raw_cookies():
    """The component must never read or display raw cookie values from
    the Provider object. Only the boolean flag has_anthropic_session_cookies
    should be referenced."""
    src = Path("frontend/src/components/providers/AnthropicBillingPanel.tsx").read_text()
    # Component reads has_anthropic_session_cookies
    assert "has_anthropic_session_cookies" in src
    # Component must NOT reference anthropic_session_cookies (without the
    # has_ prefix), which would indicate it's trying to read raw values
    # that don't exist on the API surface anyway.
    bad_patterns = [
        "provider.anthropic_session_cookies",
        "p.anthropic_session_cookies",
    ]
    for pat in bad_patterns:
        assert pat not in src, f"component must not read raw cookies: {pat}"


def test_provider_serializer_exposes_new_fields():
    """Backend serializer must expose the four new fields so the panel
    can render. Verified at the source level since the API endpoint
    isn't import-friendly in this test env."""
    src = Path("app/api/providers.py").read_text()
    assert '"anthropic_org_uuid"' in src
    assert '"has_anthropic_session_cookies"' in src
    assert '"auto_skip_until"' in src
    assert '"auto_skip_reason"' in src
