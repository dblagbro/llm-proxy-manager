"""v5.20.4 — Model cost-map sync worker + catalog-first pricing lookup."""
from __future__ import annotations
from pathlib import Path


def test_parse_entry_extracts_pricing_and_context():
    from app.monitoring.model_cost_map_worker import _parse_entry
    result = _parse_entry("gpt-4o-mini", {
        "input_cost_per_token": 1.5e-07,
        "output_cost_per_token": 6.0e-07,
        "max_input_tokens": 128000,
        "max_output_tokens": 16384,
        "litellm_provider": "openai",
        "mode": "chat",
    })
    assert result is not None
    assert result["model_key"] == "gpt-4o-mini"
    assert abs(result["input_cost_per_token"] - 1.5e-07) < 1e-12
    assert abs(result["output_cost_per_token"] - 6.0e-07) < 1e-12
    assert result["max_input_tokens"] == 128000
    assert result["max_output_tokens"] == 16384
    assert result["provider_family"] == "openai"


def test_parse_entry_skips_sample_spec():
    from app.monitoring.model_cost_map_worker import _parse_entry
    assert _parse_entry("sample_spec", {"input_cost_per_token": 0.0}) is None


def test_parse_entry_skips_entries_without_pricing():
    from app.monitoring.model_cost_map_worker import _parse_entry
    assert _parse_entry("metadata-only", {"litellm_provider": "openai"}) is None
    assert _parse_entry("nothing", {}) is None


def test_parse_entry_handles_zero_pricing():
    from app.monitoring.model_cost_map_worker import _parse_entry
    result = _parse_entry("local-llm", {
        "input_cost_per_token": 0.0,
        "output_cost_per_token": 0.0,
    })
    assert result is not None
    assert result["input_cost_per_token"] == 0.0


def test_parse_entry_handles_malformed_numeric_values():
    from app.monitoring.model_cost_map_worker import _parse_entry
    assert _parse_entry("busted", {
        "input_cost_per_token": "not-a-number",
        "output_cost_per_token": "also-bad",
    }) is None


def test_orm_class_has_expected_shape():
    from app.models.db_model_pricing import ModelPricingEntry
    columns = {c.name for c in ModelPricingEntry.__table__.columns}
    assert "model_key" in columns
    assert "input_cost_per_token" in columns
    assert "output_cost_per_token" in columns
    assert "max_input_tokens" in columns
    assert "max_output_tokens" in columns
    assert "provider_family" in columns
    assert "source" in columns
    assert "synced_at" in columns


def test_pricing_uses_catalog_first():
    from app.monitoring import pricing
    pricing.invalidate_catalog_cache()
    pricing._CATALOG_CACHE["new-model-xyz"] = (2e-06, 8e-06)
    pricing._CATALOG_LOADED = True
    in_cost, out_cost = pricing.estimate_cost_split(
        "new-model-xyz", 1_000_000, 500_000,
    )
    assert abs(in_cost - 2.0) < 1e-9
    assert abs(out_cost - 4.0) < 1e-9
    pricing.invalidate_catalog_cache()


def test_pricing_falls_through_to_litellm_when_catalog_miss():
    from app.monitoring import pricing
    pricing.invalidate_catalog_cache()
    pricing._CATALOG_CACHE.clear()
    pricing._CATALOG_LOADED = True
    in_cost, out_cost = pricing.estimate_cost_split(
        "anthropic/claude-sonnet-4-6", 1_000_000, 1_000_000,
    )
    assert in_cost > 0 or out_cost > 0
    pricing.invalidate_catalog_cache()


def test_pricing_prefix_variants_match():
    from app.monitoring import pricing
    pricing.invalidate_catalog_cache()
    pricing._CATALOG_CACHE["gpt-4o"] = (5e-06, 15e-06)
    pricing._CATALOG_LOADED = True
    in_cost, out_cost = pricing.estimate_cost_split("openai/gpt-4o", 1000, 1000)
    assert in_cost > 0
    assert out_cost > 0
    pricing.invalidate_catalog_cache()


def test_worker_registers_at_startup():
    src = Path("app/main.py").read_text()
    assert "model_cost_map_worker" in src
    assert "_start_cost_map" in src or "model_cost_map_worker.start" in src


def test_worker_invalidates_cache_after_upsert():
    src = Path("app/monitoring/model_cost_map_worker.py").read_text()
    assert "invalidate_catalog_cache" in src


def test_worker_url_configurable():
    src = Path("app/monitoring/model_cost_map_worker.py").read_text()
    assert "model_cost_map_url" in src
    assert "DEFAULT_URL" in src


def test_worker_writes_activity_log_row():
    src = Path("app/monitoring/model_cost_map_worker.py").read_text()
    assert '"model_cost_map.synced"' in src or "'model_cost_map.synced'" in src
    assert "ActivityLog" in src


def test_worker_is_daily_by_default():
    src = Path("app/monitoring/model_cost_map_worker.py").read_text()
    assert "DEFAULT_INTERVAL_SEC = 86400" in src


def test_orm_class_imported_at_init_db():
    src = Path("app/models/database.py").read_text()
    assert "db_model_pricing" in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 20, 4), (
        f"expected >= 5.20.4, got {major}.{minor}.{patch}"
    )
