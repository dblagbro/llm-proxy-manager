"""v5.7.9 — status monitor: filter Google Cloud incidents to LLM-affecting products.

Repro: the live Google Cloud status page on 2026-06-15 listed one active
incident — a network packet-loss event affecting traffic ORIGINATING
from India (Hybrid Connectivity / Media CDN / VPC). The pre-fix code
flagged any active incident as ``degraded`` and force-opened the
Vertex + Gemini circuits, even though the incident had no LLM impact.

Two bugs to pin:
1. Field name typo: ``external-desc`` (kebab) vs Google's ``external_desc``
   (snake) — every warning log emitted an empty description.
2. No filtering: any active incident → all Google providers degraded.
"""
from __future__ import annotations

import pytest

from app.monitoring import status as status_mod


@pytest.mark.asyncio
async def test_google_non_llm_incident_not_degraded(monkeypatch):
    """The India-ingress packet-loss incident has affected_products
    ['Hybrid Connectivity','Media CDN','VPC'] — none are LLM APIs.
    The monitor must NOT flag Google as degraded."""
    fake_incidents = [{
        "uri": "incidents/test-india-ingress",
        "service_name": "Multiple Products",
        "external_desc": "Network traffic to GCP from India experiencing latency",
        "affected_products": [
            {"title": "Hybrid Connectivity", "id": "x"},
            {"title": "Media CDN", "id": "y"},
            {"title": "Virtual Private Cloud (VPC)", "id": "z"},
        ],
        "begin": "2026-06-09T18:22:00+00:00",
        # no "end" → active
    }]

    class _StubResp:
        def json(self):
            return fake_incidents

    class _StubClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            return _StubResp()

    monkeypatch.setattr(status_mod, "httpx", type("H", (), {"AsyncClient": lambda *a, **k: _StubClient()})())
    status_mod._cache.clear()
    degraded, desc = await status_mod._check_one("google")
    assert degraded is False, f"India-ingress incident must NOT flag Google degraded (got desc={desc!r})"


@pytest.mark.asyncio
async def test_google_vertex_incident_flags_degraded(monkeypatch):
    """A real Vertex AI incident WITH external_desc populated MUST be
    flagged AND surface the correct description (not empty — the
    pre-fix code read external-desc with a hyphen)."""
    fake_incidents = [{
        "uri": "incidents/test-vertex",
        "service_name": "Vertex AI",
        "external_desc": "Vertex AI prediction errors in us-central1",
        "affected_products": [
            {"title": "Vertex AI", "id": "v1"},
        ],
        "begin": "2026-06-15T01:00:00+00:00",
    }]

    class _StubResp:
        def json(self):
            return fake_incidents

    class _StubClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            return _StubResp()

    monkeypatch.setattr(status_mod, "httpx", type("H", (), {"AsyncClient": lambda *a, **k: _StubClient()})())
    status_mod._cache.clear()
    degraded, desc = await status_mod._check_one("google")
    assert degraded is True
    assert "Vertex AI prediction errors" in desc


@pytest.mark.asyncio
async def test_google_generative_language_incident_flags(monkeypatch):
    """Gemini API incidents should also be caught."""
    fake_incidents = [{
        "uri": "incidents/test-gemini",
        "external_desc": "Generative Language API elevated latency",
        "affected_products": [
            {"title": "Generative Language API", "id": "g1"},
        ],
        "begin": "2026-06-15T01:00:00+00:00",
    }]

    class _StubResp:
        def json(self):
            return fake_incidents

    class _StubClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            return _StubResp()

    monkeypatch.setattr(status_mod, "httpx", type("H", (), {"AsyncClient": lambda *a, **k: _StubClient()})())
    status_mod._cache.clear()
    degraded, desc = await status_mod._check_one("google")
    assert degraded is True
    assert "Generative Language API" in desc


@pytest.mark.asyncio
async def test_google_no_active_incidents_clean(monkeypatch):
    fake_incidents = []  # All resolved

    class _StubResp:
        def json(self):
            return fake_incidents

    class _StubClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            return _StubResp()

    monkeypatch.setattr(status_mod, "httpx", type("H", (), {"AsyncClient": lambda *a, **k: _StubClient()})())
    status_mod._cache.clear()
    degraded, desc = await status_mod._check_one("google")
    assert degraded is False
    assert desc == ""
