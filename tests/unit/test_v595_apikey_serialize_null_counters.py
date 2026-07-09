"""v5.9.5 — `_serialize` must coalesce NULL counter columns to 0/0.0.

A never-used api_key has `total_requests`/`total_tokens`/`total_cost_usd`
as raw NULL in SQLite. Pre-v5.9.5 the serializer returned those NULLs
as JSON `null`, and the frontend's `APIKeysPage.tsx` line 646 renders
`{k.total_requests.toLocaleString()}` without a null guard. A single
NULL row was enough to throw `TypeError: Cannot read properties of
null` and crash the whole /keys page to a flash-then-white.
"""
from __future__ import annotations

from types import SimpleNamespace


def _make_key(**overrides):
    """Minimal stand-in for an ApiKey row — only the fields _serialize
    touches. NULL values flow through to the frontend as JSON null."""
    base = dict(
        id="k1", name="x", key_prefix="abc", key_type="standard",
        enabled=True,
        total_requests=None, total_tokens=None, total_cost_usd=None,
        spending_cap_usd=None, rate_limit_rpm=None,
        rate_limit_tier=None,
        daily_soft_cap_usd=None, daily_hard_cap_usd=None,
        hourly_cap_usd=None,
        semantic_cache_enabled=False, caller_memory_ttl_days=None,
        blocked_companies=None, allowed_paths=None,
        allowed_companies=None, blocked_models=None, allowed_models=None,
        debug_echo_enabled=False,
        day_cost_usd=None, hour_cost_usd=None,
        encrypted_key=None, last_used_at=None,
        created_at=None, deleted_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_serialize_coalesces_null_counters_to_zero():
    from app.api.apikeys import _serialize
    out = _serialize(_make_key())
    assert out["total_requests"] == 0
    assert out["total_tokens"] == 0
    assert out["total_cost_usd"] == 0.0
    # Types matter — frontend does `(0).toLocaleString()` (number)
    # not `("0").toLocaleString()` (string).
    assert isinstance(out["total_requests"], int)
    assert isinstance(out["total_tokens"], int)
    assert isinstance(out["total_cost_usd"], float)


def test_serialize_preserves_non_null_counter_values():
    from app.api.apikeys import _serialize
    out = _serialize(_make_key(
        total_requests=237605,
        total_tokens=987_654_321,
        total_cost_usd=12.345,
    ))
    assert out["total_requests"] == 237605
    assert out["total_tokens"] == 987_654_321
    assert out["total_cost_usd"] == 12.345
