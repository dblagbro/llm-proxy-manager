"""v4.4.21 — per-node Provider Summary.

``provider_metrics`` is per-node (not cluster-replicated, despite a
stale 2026-05-15 backlog note claiming otherwise). The existing
``/api/monitoring/metrics`` therefore shows only the local node's
slice of the cluster's traffic. v4.4.21 adds:

- ``GET /cluster/local-metrics`` — HMAC-authed; returns this node's
  summary plus its ``node_id`` label
- ``GET /api/monitoring/metrics-by-node`` — admin-authed fan-out
  that calls the cluster endpoint on each peer and returns
  ``{nodes: [{node_id, ok, providers}, …]}``
- ``node_id`` field on the existing ``/api/monitoring/metrics``
  response so the UI can label the aggregate view

These tests are source/shape guards — exercising the live fan-out
against in-process peers would require a full multi-node test
harness; the cross-cluster HTTP shape is exercised by the existing
``/cluster/oauth-pull`` integration tests.
"""
from __future__ import annotations

from pathlib import Path


def test_cluster_local_metrics_endpoint_exists():
    src = Path("app/api/cluster.py").read_text()
    assert '"/cluster/local-metrics"' in src or "/cluster/local-metrics" in src, (
        "GET /cluster/local-metrics endpoint missing"
    )
    # HMAC-authed (matches the existing /cluster/oauth-pull pattern)
    block_start = src.index("/cluster/local-metrics")
    block = src[block_start:block_start + 1800]
    assert "X-Cluster-Node" in block, "endpoint must enforce HMAC node header"
    assert "X-Cluster-Sig" in block, "endpoint must enforce HMAC signature header"
    assert "verify_payload" in block, "endpoint must verify HMAC signature"


def test_local_metrics_returns_node_id():
    """The cluster endpoint payload must include node_id so the
    fan-out wrapper can key results by node without trusting the
    URL it dialed."""
    src = Path("app/api/cluster.py").read_text()
    idx = src.index("/cluster/local-metrics")
    block = src[idx:idx + 1800]
    assert '"node_id"' in block, "payload must contain node_id"


def test_metrics_by_node_endpoint_exists():
    src = Path("app/api/monitoring.py").read_text()
    assert "/metrics-by-node" in src, "/api/monitoring/metrics-by-node missing"
    idx = src.index("metrics-by-node")
    block = src[idx:idx + 4000]
    # Admin-authed (re-uses the require_admin dep on this router)
    assert "require_admin" in src, "admin auth dep must exist on this router"
    # Fan-out structure
    assert "_parse_peers" in block, "fan-out must call _parse_peers"
    assert "sign_payload" in block, "fan-out must HMAC-sign its peer calls"
    # Partial-view tolerance: unreachable peers must not fail the whole call
    assert '"ok": False' in block or "ok=False" in block or '"ok":False' in block, (
        "must report unreachable peers via ok=False, not raise"
    )


def test_existing_metrics_endpoint_adds_node_id():
    """The existing /api/monitoring/metrics response gets a node_id
    field so the dashboard can label the aggregate view."""
    src = Path("app/api/monitoring.py").read_text()
    # Find the metrics_summary function
    idx = src.index("async def metrics_summary")
    block = src[idx:idx + 1500]
    assert "node_id" in block, (
        "existing /metrics endpoint must include node_id so the UI can "
        "label which node it's viewing"
    )


def test_frontend_types_have_metrics_by_node_response():
    src = Path("frontend/src/types/index.ts").read_text()
    assert "MetricsByNodeResponse" in src, "frontend type for the new endpoint missing"
    assert "MetricsByNodeNode" in src, "frontend per-node row type missing"
    # The new field on the existing type
    assert "node_id?: string" in src, "MetricsSummary must gain node_id?"


def test_frontend_api_client_has_metricsByNode():
    src = Path("frontend/src/api/index.ts").read_text()
    assert "metricsByNode" in src, "monitoringApi.metricsByNode missing"
    assert "/api/monitoring/metrics-by-node" in src, (
        "client must hit the new backend path"
    )


def test_metrics_page_has_per_node_toggle():
    src = Path("frontend/src/pages/MetricsPage.tsx").read_text()
    assert "showPerNode" in src, "per-node toggle state missing"
    assert "Show per-node" in src, "per-node toggle button label missing"
    # Lazy-load gate so the fan-out doesn't run on every page load
    assert "enabled: showPerNode" in src, "by-node query must be gated on toggle"


def test_metrics_page_shows_partial_view_on_unreachable_peer():
    """The render path must handle ok=false rows by surfacing the
    error, not silently dropping or crashing on missing .providers."""
    src = Path("frontend/src/pages/MetricsPage.tsx").read_text()
    idx = src.index("Per-node Breakdown")
    block = src[idx:idx + 5000]
    assert "node unreachable" in block, (
        "render path must label peers that returned ok=false"
    )
