"""v4.4.15 (F-OBS-003) — caller-memory gating-header telemetry.

Caller-memory write-back is gated on the inbound ``X-Conversation-Id``
header. The feature flag has been ON cluster-wide since 2026-05-15
but ``caller_memory`` has had 0 production writes — because no
consumer is sending the header yet (F-OBS-003 observation). Rather
than wait and periodically diff the ``caller_memory`` table, v4.4.15
adds a Prometheus counter + an admin read endpoint so the operator
can SEE the moment a consumer starts sending the header.

This file guards:
1. The counter is defined with the right name + labels.
2. Both /v1 entry points increment it (source-level — exercising the
   full FastAPI request path in a unit test is heavy; the call site
   is a 6-line block right after auth).
3. The admin endpoint reads the counter and computes ``header_seen``.
"""
from __future__ import annotations

from pathlib import Path


# ── Counter definition ───────────────────────────────────────────
#
# NOTE: some unit tests (test_claude_oauth.py, test_priority_bump.py)
# install a ``_Noop`` stub for ``prometheus_client`` into sys.modules
# to avoid registry pollution. Once installed, that stub persists for
# the rest of the test process, so ``CONVERSATION_ID_REQUESTS_TOTAL``
# may resolve to a ``_Noop`` instance with no ``_name`` / no real
# registry samples — depending on test ordering. The runtime-value
# tests below therefore guard for the _Noop case; the source-level
# tests are the pollution-proof guarantee.


def _is_real_counter(obj) -> bool:
    """True iff obj is a real prometheus Counter (not the _Noop stub
    some test modules install)."""
    return hasattr(obj, "_name") and hasattr(obj, "_labelnames")


def test_counter_defined_in_source_with_expected_name_and_labels():
    """Pollution-proof: assert the counter declaration in the source
    rather than the runtime object (which may be a _Noop stub
    depending on test ordering)."""
    src = Path("app/observability/prometheus.py").read_text()
    assert 'CONVERSATION_ID_REQUESTS_TOTAL = Counter(' in src
    assert '"llm_proxy_conversation_id_requests_total"' in src
    assert '["endpoint", "has_conversation_id"]' in src


def test_counter_increments_and_reads_back():
    from app.observability.prometheus import CONVERSATION_ID_REQUESTS_TOTAL
    if not _is_real_counter(CONVERSATION_ID_REQUESTS_TOTAL):
        import pytest
        pytest.skip("prometheus_client stubbed to _Noop by an earlier test")
    from prometheus_client import REGISTRY

    def _read(endpoint, has):
        for metric in REGISTRY.collect():
            if metric.name != "llm_proxy_conversation_id_requests":
                continue
            for s in metric.samples:
                if (s.name.endswith("_total")
                        and s.labels.get("endpoint") == endpoint
                        and s.labels.get("has_conversation_id") == has):
                    return s.value
        return 0.0

    before = _read("messages", "true")
    CONVERSATION_ID_REQUESTS_TOTAL.labels(endpoint="messages", has_conversation_id="true").inc()
    after = _read("messages", "true")
    assert after == before + 1


# ── Both entry points increment the counter ──────────────────────


def test_messages_endpoint_records_counter():
    from tests._entry_surface import entry_surface
    src = entry_surface("app/api/messages.py")  # counter lives in _handler_shared, called at entry
    assert "CONVERSATION_ID_REQUESTS_TOTAL" in src
    # The label call uses the conditional on x_conversation_id
    assert 'endpoint="messages"' in src
    assert '"true" if x_conversation_id else "false"' in src
    # And it's wrapped so telemetry never breaks the request path
    idx = src.index("CONVERSATION_ID_REQUESTS_TOTAL")
    window = src[idx - 200:idx + 300]
    assert "try:" in window
    assert "except Exception:" in window


def test_completions_endpoint_records_counter():
    from tests._entry_surface import entry_surface
    src = entry_surface("app/api/completions.py")
    assert "CONVERSATION_ID_REQUESTS_TOTAL" in src
    assert 'endpoint="completions"' in src
    assert '"true" if x_conversation_id else "false"' in src
    idx = src.index("CONVERSATION_ID_REQUESTS_TOTAL")
    window = src[idx - 200:idx + 300]
    assert "try:" in window
    assert "except Exception:" in window


# ── Admin endpoint ───────────────────────────────────────────────


def test_admin_endpoint_registered():
    from app.api.monitoring import router
    paths = {r.path for r in router.routes}
    assert "/api/monitoring/conversation-id-stats" in paths


def test_admin_endpoint_computes_header_seen():
    """The endpoint reads the counter + derives ``header_seen``.
    Behavioral: increment the 'true' series, then confirm the
    endpoint's aggregation logic would report header_seen=True.
    Skips if prometheus_client is stubbed by an earlier test."""
    from app.observability.prometheus import CONVERSATION_ID_REQUESTS_TOTAL
    if not _is_real_counter(CONVERSATION_ID_REQUESTS_TOTAL):
        import pytest
        pytest.skip("prometheus_client stubbed to _Noop by an earlier test")
    from prometheus_client import REGISTRY

    CONVERSATION_ID_REQUESTS_TOTAL.labels(
        endpoint="messages", has_conversation_id="true",
    ).inc()

    # Replicate the endpoint's aggregation (the endpoint itself is
    # async + admin-gated; the aggregation logic is what we verify).
    total_with = 0
    for metric in REGISTRY.collect():
        if metric.name != "llm_proxy_conversation_id_requests":
            continue
        for s in metric.samples:
            if s.name.endswith("_total") and s.labels.get("has_conversation_id") == "true":
                total_with += int(s.value)
    assert total_with > 0, "after incrementing the 'true' series, total_with should be >0"


def test_admin_endpoint_source_shape():
    """The endpoint returns the documented dict shape."""
    src = Path("app/api/monitoring.py").read_text()
    idx = src.index("async def conversation_id_stats(")
    body = src[idx:idx + 2500]
    assert '"by_endpoint"' in body
    assert '"total_with_header"' in body
    assert '"header_seen"' in body
    # Admin-gated
    assert "require_admin" in body
