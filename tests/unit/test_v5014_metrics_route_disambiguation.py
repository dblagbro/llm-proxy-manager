"""v5.0.14 — ``/metrics`` content-negotiates between SPA and Prometheus.

Pre-v5.0.14 the bare ``/metrics`` path always returned Prometheus
text/plain, even when a browser typed
``https://www.voipguru.org/llm-proxy2/metrics`` expecting the React
MetricsPage. The operator saw raw Prometheus output instead of the
UI — broken since the SPA route was added but only surfaced today
when the operator hit it via a fresh tab rather than via in-app
navigation.

The fix is Accept-header sniffing:

  - ``Accept: text/html,…``  → serve index.html (React Router takes over)
  - ``Accept: */*`` /
    ``Accept: text/plain;…``  → Prometheus text (existing behavior)

External Prometheus scrapers (Grafana Cloud Agent, vmagent,
prometheus itself) all default to ``Accept: */*`` or an explicit
``text/plain;version=0.0.4`` — none send ``text/html``. Browsers
typing the URL always include ``text/html`` first.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Source guard ────────────────────────────────────────────────────


def test_metrics_route_sniffs_accept_header():
    """The metrics() handler must branch on the Accept header. Catches
    a future edit that reverts to always returning Prometheus."""
    src = Path("app/main.py").read_text()
    # Find the handler block
    assert "@app.get(\"/metrics\", include_in_schema=False)" in src
    # Branch must reference Accept + text/html
    idx = src.index("@app.get(\"/metrics\", include_in_schema=False)")
    block = src[idx:idx + 1800]
    assert "accept" in block.lower(), (
        "metrics handler no longer reads the Accept header — browsers "
        "hitting /llm-proxy2/metrics will get Prometheus text again."
    )
    assert "text/html" in block, (
        "metrics handler must check for text/html in the Accept header "
        "to decide between SPA and Prometheus."
    )
    assert "metrics_response" in block, (
        "Prometheus fallback path lost — non-browser callers would no "
        "longer get the scrape output."
    )


# ── Behavioral ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_browser_accept_serves_spa(tmp_path, monkeypatch):
    """Browser-style Accept header → SPA index.html (200 + text/html)."""
    from fastapi import Request
    from fastapi.responses import FileResponse
    from app import main as appmain

    # Pretend the SPA bundle exists at the expected path so the
    # handler can serve it.
    fake_static = tmp_path / "frontend" / "dist"
    fake_static.mkdir(parents=True)
    (fake_static / "index.html").write_text(
        "<!doctype html><html><body>SPA</body></html>"
    )
    monkeypatch.setattr(appmain, "_static_dir", str(fake_static))

    req = Request(scope={
        "type": "http",
        "headers": [(b"accept", b"text/html,application/xhtml+xml,*/*;q=0.8")],
    })
    resp = await appmain.metrics(req)
    assert isinstance(resp, FileResponse), (
        f"Browser Accept did not get SPA shell — got {type(resp).__name__}; "
        "operators typing /llm-proxy2/metrics will see Prometheus text."
    )


@pytest.mark.asyncio
async def test_metrics_prometheus_accept_serves_text():
    """Prometheus scrape Accept (``*/*`` or ``text/plain``) → Prometheus
    text/plain response. Regression guards against breaking external
    scrapers that depend on the well-known /metrics endpoint."""
    from fastapi import Request
    from app import main as appmain

    # Default Prometheus client sends Accept: */* OR an explicit
    # text/plain;version=0.0.4 — neither includes text/html.
    for accept_value in (
        b"*/*",
        b"text/plain;version=0.0.4;charset=utf-8,application/openmetrics-text;version=1.0.0;charset=utf-8",
    ):
        req = Request(scope={
            "type": "http",
            "headers": [(b"accept", accept_value)],
        })
        resp = await appmain.metrics(req)
        # Prometheus response has content-type text/plain*
        ctype = resp.headers.get("content-type", "")
        assert "text/plain" in ctype or "openmetrics" in ctype, (
            f"Prometheus-style Accept {accept_value!r} got content-type "
            f"{ctype!r} — external scrapers may parse it wrong."
        )


@pytest.mark.asyncio
async def test_metrics_no_accept_header_defaults_to_prometheus():
    """A request with NO Accept header (rare but happens with bare
    httpie / wget) falls through to the Prometheus path. Important so
    a misconfigured scraper still gets data rather than HTML."""
    from fastapi import Request
    from app import main as appmain

    req = Request(scope={"type": "http", "headers": []})
    resp = await appmain.metrics(req)
    ctype = resp.headers.get("content-type", "")
    assert "text/plain" in ctype or "openmetrics" in ctype, (
        f"No-Accept request fell through to SPA instead of Prometheus "
        f"(got {ctype!r}). Bare wget/httpie scrapers would break."
    )
