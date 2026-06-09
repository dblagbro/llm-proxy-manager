"""v5.3.2 — GET /api/compliance/taxonomy lightweight company list for
the frontend policy-editor dropdowns. Closes a v5.2 audit deferral
(``docs/v5.2-vendor-neutrality-compliance-report.md`` risks #1+#2 —
custom-companies added via ``COMPLIANCE_CUSTOM_COMPANIES`` were
invisible to the WebUI because ``frontend/src/types/index.ts``
hardcoded the company list).

Tests bypass FastAPI's auth dependency by calling the endpoint
function directly with a stub admin — avoids cross-test pollution of
``dependency_overrides`` when run inside the full unit suite.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def test_endpoint_registered():
    src = Path("app/api/compliance.py").read_text()
    assert '/api/compliance/taxonomy' in src
    assert 'def compliance_taxonomy' in src
    # Admin-gated (parity with the rest of the policy surface)
    idx = src.find('def compliance_taxonomy')
    head = src[idx:idx + 400]
    assert 'require_admin' in head


@pytest.mark.asyncio
async def test_returns_known_companies():
    from app.api.compliance import compliance_taxonomy

    body = await compliance_taxonomy(_=None)  # admin already injected at the framework layer

    assert "companies" in body
    ids = [c["id"] for c in body["companies"]]
    for expected in ("anthropic", "openai", "google", "xai", "cohere"):
        assert expected in ids, f"missing {expected}"
    for row in body["companies"]:
        assert "id" in row and isinstance(row["id"], str)
        assert "label" in row and isinstance(row["label"], str)
        assert row["source"] in ("known", "custom")


@pytest.mark.asyncio
async def test_sorted_by_label():
    """Stable rendering — case-insensitive sort by label so the UI
    dropdown order doesn't shift between requests."""
    from app.api.compliance import compliance_taxonomy

    body = await compliance_taxonomy(_=None)
    labels = [c["label"].lower() for c in body["companies"]]
    assert labels == sorted(labels), \
        f"taxonomy not sorted by label: {labels}"


@pytest.mark.asyncio
async def test_custom_companies_surface_with_source_custom():
    """COMPLIANCE_CUSTOM_COMPANIES JSON entries must show up in the
    response with source='custom'. Closes the v5.2 audit deferral."""
    from app.api.compliance import compliance_taxonomy
    from app.config import settings as real_settings

    custom_json = json.dumps([
        {"id": "deepseek", "display_name": "DeepSeek", "model_prefixes": ["deepseek-"]},
    ])
    with patch.object(real_settings, "compliance_custom_companies", custom_json):
        body = await compliance_taxonomy(_=None)

    ids = {c["id"]: c for c in body["companies"]}
    assert "deepseek" in ids, f"custom company not in taxonomy: {ids.keys()}"
    assert ids["deepseek"]["label"] == "DeepSeek"
    assert ids["deepseek"]["source"] == "custom"


@pytest.mark.asyncio
async def test_known_entries_not_overridden_by_custom():
    """A custom entry with id colliding a KNOWN_COMPANIES id MUST NOT
    appear twice and MUST NOT override the known entry's source.
    Mirrors the runtime resolver's precedence (decision 12)."""
    from app.api.compliance import compliance_taxonomy
    from app.config import settings as real_settings

    custom_json = json.dumps([
        {"id": "anthropic", "display_name": "NotAnthropic"},
    ])
    with patch.object(real_settings, "compliance_custom_companies", custom_json):
        body = await compliance_taxonomy(_=None)

    rows = [c for c in body["companies"] if c["id"] == "anthropic"]
    assert len(rows) == 1, f"duplicate anthropic rows: {rows}"
    assert rows[0]["source"] == "known"  # the override is ignored
