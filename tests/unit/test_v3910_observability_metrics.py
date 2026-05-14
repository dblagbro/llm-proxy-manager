"""v3.9.10 — Prometheus metrics for caller-memory + pool + scrape freshness.

Source-level guards confirming the metric primitives are defined, the
helpers are wired into the memory modules, and the background sampler
exists with sensible defaults.
"""
from __future__ import annotations

from pathlib import Path


# ── Prometheus primitives exist ────────────────────────────────────


def test_memory_operations_counter_defined():
    from app.observability.prometheus import MEMORY_OPERATIONS_TOTAL
    # Counter with labels (operation, outcome) — call .labels() to confirm shape
    sample = MEMORY_OPERATIONS_TOTAL.labels(operation="inject", outcome="applied")
    assert sample is not None


def test_db_pool_gauges_defined():
    from app.observability.prometheus import (
        DB_POOL_CHECKED_OUT, DB_POOL_OVERFLOW, DB_POOL_SIZE,
    )
    assert DB_POOL_CHECKED_OUT is not None
    assert DB_POOL_OVERFLOW is not None
    assert DB_POOL_SIZE is not None


def test_scrape_freshness_gauge_defined():
    from app.observability.prometheus import SCRAPE_FRESHNESS_SECONDS
    sample = SCRAPE_FRESHNESS_SECONDS.labels(
        provider_id="p1", provider_name="Test", source="anthropic_console_v1",
    )
    assert sample is not None


def test_observer_helpers_callable():
    from app.observability.prometheus import (
        observe_memory_operation,
        observe_db_pool_snapshot,
        observe_scrape_freshness,
    )
    # Smoke — should not raise
    observe_memory_operation("inject", "applied")
    observe_db_pool_snapshot(50, 0, 0)
    observe_scrape_freshness("p1", "Test", "anthropic_console_v1", 1234.5)


# ── Memory modules wired ───────────────────────────────────────────


def test_inject_emits_metrics():
    src = Path("app/memory/inject.py").read_text()
    assert "observe_memory_operation" in src
    # Both success outcomes ("applied") and the silent-degrade path ("degraded")
    assert '"applied"' in src
    assert '"degraded"' in src


def test_extract_emits_metrics():
    src = Path("app/memory/extract.py").read_text()
    assert "observe_memory_operation" in src
    assert '"applied"' in src
    assert '"degraded"' in src


def test_flush_emits_metrics():
    src = Path("app/memory/flush.py").read_text()
    assert "observe_memory_operation" in src
    assert '"degraded"' in src


def test_recover_emits_metrics():
    src = Path("app/memory/recover.py").read_text()
    assert "observe_memory_operation" in src
    # Both applied + skipped + degraded paths
    assert '"applied"' in src
    assert '"degraded"' in src


# ── Background sampler ─────────────────────────────────────────────


def test_observability_sampler_module_exists():
    import importlib
    mod = importlib.import_module("app.monitoring.observability_sampler")
    assert hasattr(mod, "start")
    assert hasattr(mod, "_sample_pool")
    assert hasattr(mod, "_sample_scrape_freshness")


def test_sampler_started_in_main():
    src = Path("app/main.py").read_text()
    assert "observability_sampler" in src
    assert "_obs_sampler.start()" in src


def test_sampler_default_interval_is_30s():
    src = Path("app/monitoring/observability_sampler.py").read_text()
    assert "_INTERVAL_SEC = 30" in src


def test_sampler_uses_engine_pool_snapshot():
    src = Path("app/monitoring/observability_sampler.py").read_text()
    assert "from app.models.database import engine" in src
    assert "pool.checkedout()" in src
    assert "pool.overflow()" in src


def test_sampler_only_emits_for_scraped_providers():
    """Don't pollute /metrics with infinity gauges for providers that have
    never been scraped — defer until they have at least one snapshot."""
    src = Path("app/monitoring/observability_sampler.py").read_text()
    assert "if snap is None or snap.captured_at is None:" in src
    assert "continue" in src
