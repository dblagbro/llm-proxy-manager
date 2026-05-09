"""v3.3.5 — grok-web conversation rotation pool.

Validates that ``_pick_conversation_id`` round-robins across
``conversation_ids`` (list) when populated and falls back to the
single ``conversation_id`` for back-compat.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_rotation_counter():
    """Each test gets a clean rotation counter."""
    from app.providers import grok_web
    grok_web._rotation_counter.clear()
    yield
    grok_web._rotation_counter.clear()


def test_pick_returns_single_conv_when_no_pool():
    """v3.2.x back-compat: only ``conversation_id`` set → return it."""
    from app.providers.grok_web import _pick_conversation_id
    cfg = {"conversation_id": "uuid-A"}
    assert _pick_conversation_id(cfg) == "uuid-A"
    # Repeated calls keep returning the same single conv (no rotation)
    assert _pick_conversation_id(cfg) == "uuid-A"
    assert _pick_conversation_id(cfg) == "uuid-A"


def test_pick_rotates_through_pool():
    """conversation_ids list → round-robin across the pool."""
    from app.providers.grok_web import _pick_conversation_id
    cfg = {"conversation_ids": ["uuid-A", "uuid-B", "uuid-C"]}
    seq = [_pick_conversation_id(cfg) for _ in range(7)]
    # First three calls cover all three; then it wraps
    assert seq[0:3] == ["uuid-A", "uuid-B", "uuid-C"]
    assert seq[3:6] == ["uuid-A", "uuid-B", "uuid-C"]
    assert seq[6] == "uuid-A"


def test_pick_pool_takes_precedence_over_single():
    """When both are set, the pool wins (single is ignored)."""
    from app.providers.grok_web import _pick_conversation_id
    cfg = {
        "conversation_id": "single",
        "conversation_ids": ["pool-1", "pool-2"],
    }
    seq = [_pick_conversation_id(cfg) for _ in range(4)]
    assert seq == ["pool-1", "pool-2", "pool-1", "pool-2"]
    assert "single" not in seq


def test_pick_empty_pool_falls_back_to_single():
    """Empty list shouldn't crash — falls through to single."""
    from app.providers.grok_web import _pick_conversation_id
    cfg = {"conversation_id": "fallback", "conversation_ids": []}
    assert _pick_conversation_id(cfg) == "fallback"


def test_pick_pool_with_invalid_entries_skips_them():
    """Non-string / empty pool entries don't break the rotation."""
    from app.providers.grok_web import _pick_conversation_id
    cfg = {
        "conversation_id": "fallback",
        "conversation_ids": ["", None, "valid-1"],
    }
    # Whatever index we land on, an empty/None entry triggers the
    # back-compat fallback to the single id; "valid-1" returns directly.
    seen = set()
    for _ in range(6):
        seen.add(_pick_conversation_id(cfg))
    assert "valid-1" in seen
    # Empty/None entries should never leak through
    assert "" not in seen
    assert None not in seen


def test_pick_returns_empty_when_nothing_configured():
    """Neither field set → empty string (caller's validator should
    have already raised before this point)."""
    from app.providers.grok_web import _pick_conversation_id
    assert _pick_conversation_id({}) == ""
    assert _pick_conversation_id(None) == ""


def test_validate_extra_config_accepts_pool_only():
    """Provider config with only conversation_ids passes validation."""
    from app.providers.grok_web import _validate_extra_config
    # Bridge mode
    _validate_extra_config({
        "bridge_url": "http://bridge:8443",
        "conversation_ids": ["uuid-A"],
    })  # no raise
    # Manual mode
    _validate_extra_config({
        "cookie_header": "cf=...",
        "conversation_ids": ["uuid-A", "uuid-B"],
    })  # no raise


def test_validate_extra_config_accepts_single_only():
    """v3.2.x back-compat — single conversation_id still valid."""
    from app.providers.grok_web import _validate_extra_config
    _validate_extra_config({
        "cookie_header": "cf=...",
        "conversation_id": "uuid-A",
    })  # no raise


def test_validate_extra_config_rejects_no_conversation():
    """Neither single nor list set → 400."""
    from app.providers.grok_web import _validate_extra_config, GrokWebError
    with pytest.raises(GrokWebError) as exc:
        _validate_extra_config({"cookie_header": "cf=..."})
    assert "conversation" in str(exc.value).lower()
    assert exc.value.status_code == 400


def test_validate_extra_config_rejects_empty_pool():
    """Empty conversation_ids list with no fallback → 400."""
    from app.providers.grok_web import _validate_extra_config, GrokWebError
    with pytest.raises(GrokWebError):
        _validate_extra_config({
            "cookie_header": "cf=...",
            "conversation_ids": [],
        })


def test_rotation_state_isolated_per_provider():
    """Two different providers (different extra_config dicts) keep
    independent rotation positions — shared counter dict keyed by id()
    keeps them from interfering."""
    from app.providers.grok_web import _pick_conversation_id
    cfg_a = {"conversation_ids": ["a1", "a2"]}
    cfg_b = {"conversation_ids": ["b1", "b2", "b3"]}
    # Interleave calls
    assert _pick_conversation_id(cfg_a) == "a1"
    assert _pick_conversation_id(cfg_b) == "b1"
    assert _pick_conversation_id(cfg_a) == "a2"
    assert _pick_conversation_id(cfg_b) == "b2"
    assert _pick_conversation_id(cfg_a) == "a1"  # wrapped
    assert _pick_conversation_id(cfg_b) == "b3"
