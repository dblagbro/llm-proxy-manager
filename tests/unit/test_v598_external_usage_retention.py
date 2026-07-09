"""v5.9.8 (#472) — external_usage_snapshot retention.

Pre-fix, only activity_log + provider_metrics + run_events + AI-review
tables were pruned. external_usage_snapshot accumulates one row per
provider per ~4-hour scrape window (~6/day per provider). On the clone
cluster (11 providers) that's ~66 rows/day = ~24k rows/year of
observational data — only the latest snapshot per provider is consumed
by rotation logic.

These tests assert:
1. The new helper exists with the correct signature.
2. The retention default is 90 days.
3. The sweep output dict carries the new keys.
"""
from __future__ import annotations


def test_helper_exists() -> None:
    """Regression: the prune sweep must include the new helper after
    v5.9.8 so an accidental removal doesn't silently regress."""
    import inspect
    from app.monitoring import prune

    assert hasattr(prune, "_prune_external_usage_snapshots")
    assert inspect.iscoroutinefunction(prune._prune_external_usage_snapshots)
    assert hasattr(prune, "_external_usage_retention_days")


def test_default_retention_is_90_days() -> None:
    from app.monitoring import prune

    # No env override → returns the documented default.
    assert prune._external_usage_retention_days() == 90
    assert prune._DEFAULT_EXTERNAL_USAGE_RETENTION_DAYS == 90


def test_sweep_log_contains_new_field() -> None:
    """The prune.swept log line is the operator's heartbeat that the
    new table is being swept. The format-string must reference the new
    counter and keep_days so an absence is immediately obvious."""
    import inspect
    from app.monitoring import prune

    src = inspect.getsource(prune._prune_loop)
    assert "external_usage_snapshots=%d" in src
    assert "external_usage_keep_days=%d" in src


def test_sweep_out_dict_has_new_keys() -> None:
    """Assert the sweep result dict declares the new fields up front so
    a get_last_sweep() snapshot returned mid-sweep still has them."""
    import inspect
    from app.monitoring import prune

    src = inspect.getsource(prune._sweep_once)
    assert '"external_usage_snapshots":' in src
    assert '"external_usage_keep_days":' in src
