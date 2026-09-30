"""v5.9.4 — `/cluster` (and `/metrics`) must serve the SPA shell.

Pre-v5.9.4 the SPA catch-all's API-namespace denylist included bare
``cluster`` / ``lmrh`` / ``metrics`` / ``health`` / ``version``. Those
prefixes are also legit SPA routes; the bare `/cluster` (and
`/metrics`) requests returned JSON 404 instead of the SPA shell on
hard-refresh.
"""
from __future__ import annotations

# v5.22.38 — these assert the SPA catch-all serves index.html, which app/main.py
# only registers when ``frontend/dist`` exists. That directory is a build
# artifact and is gitignored, so a clean runner has none and the routes are
# genuinely absent — the tests were right to fail, they just failed for a reason
# nobody could see until CI started running them. CI now builds the frontend
# (see .github/workflows/ci.yml), so they run there; this skip keeps a developer
# who has not built it from seeing a mystery failure.
from pathlib import Path

import pytest as _pytest
from fastapi.testclient import TestClient

_DIST_INDEX = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html"
frontend_dist_required = _pytest.mark.skipif(
    not _DIST_INDEX.is_file(),
    reason=(
        "needs a built frontend (frontend/dist/index.html) — the SPA catch-all "
        "is only registered when it exists. Run `npm --prefix frontend ci && "
        "npm --prefix frontend run build`."
    ),
)


def _client():
    from app.main import app
    return TestClient(app)


@frontend_dist_required
def test_bare_cluster_serves_spa_shell_not_json_404():
    c = _client()
    r = c.get("/cluster")
    assert r.status_code == 200, (
        f"GET /cluster should serve SPA shell (200), got {r.status_code}"
    )
    assert "text/html" in r.headers.get("content-type", "").lower(), (
        "GET /cluster should be HTML (SPA shell), not JSON 404"
    )


def test_cluster_subpath_still_returns_json_404():
    """`/cluster/anything` is an API path — when no router matches it
    must return JSON 404 (not the SPA shell). The cluster_router
    serves explicit endpoints under `/cluster/status` etc.; unknown
    subpaths must keep 404'ing as JSON so non-browser clients see it
    as an error."""
    c = _client()
    r = c.get("/cluster/this-route-does-not-exist-12345")
    assert r.status_code == 404
    assert "application/json" in r.headers.get("content-type", "").lower()


@frontend_dist_required
def test_bare_metrics_serves_spa_shell():
    c = _client()
    r = c.get("/metrics-spa-route-canary")
    # /metrics-spa-route-canary doesn't start with /metrics/ — uses the
    # SPA catch-all. Sanity-check it's a 200 HTML response.
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "").lower()


def test_v1_bare_still_returns_json_404():
    """`/v1` and `/api` must keep 404'ing bare — they're never SPA
    routes. This pins the v5.9.4 narrowing didn't accidentally
    loosen the wrong prefixes."""
    c = _client()
    r = c.get("/v1")
    assert r.status_code == 404
    assert "application/json" in r.headers.get("content-type", "").lower()
    r = c.get("/api")
    assert r.status_code == 404
    assert "application/json" in r.headers.get("content-type", "").lower()
