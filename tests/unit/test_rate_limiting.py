"""Unit tests for sliding-window rate limiter in app.auth.keys."""
import collections
import time
import pytest
from unittest.mock import patch
from fastapi import HTTPException

import app.auth.keys as keys_module
from app.auth.keys import _check_rate_limit


@pytest.fixture(autouse=True)
def clear_windows():
    keys_module._rpm_windows.clear()
    yield
    keys_module._rpm_windows.clear()


def _patch_nodes(n: int):
    # Patches the function actually called by _check_rate_limit (moved to
    # app/auth/rate_limit_state.py in 2026-04-23 refactor).
    return patch("app.auth.rate_limit_state.active_node_count", return_value=n)


# ── basic behaviour ───────────────────────────────────────────────────────────

def test_first_request_allowed():
    with _patch_nodes(1):
        _check_rate_limit("k1", 10)  # must not raise


def test_requests_within_limit_all_pass():
    with _patch_nodes(1):
        for _ in range(5):
            _check_rate_limit("k1", 10)  # limit=10, 5 requests — all pass


def test_exactly_at_limit_passes():
    with _patch_nodes(1):
        for _ in range(3):
            _check_rate_limit("k1", 3)  # 3rd request should still pass


def test_one_over_limit_raises_429():
    with _patch_nodes(1):
        for _ in range(3):
            _check_rate_limit("k1", 3)
        with pytest.raises(HTTPException) as exc:
            _check_rate_limit("k1", 3)
        assert exc.value.status_code == 429


def test_429_message_mentions_rate_limit():
    with _patch_nodes(1):
        for _ in range(2):
            _check_rate_limit("k1", 2)
        with pytest.raises(HTTPException) as exc:
            _check_rate_limit("k1", 2)
        assert "rate limit" in str(exc.value.detail).lower()


# ── window expiry ─────────────────────────────────────────────────────────────

def test_expired_requests_not_counted():
    # Pre-fill window with old timestamps (>60s ago)
    old = time.monotonic() - 61.0
    keys_module._rpm_windows["k2"] = collections.deque([old, old, old])
    with _patch_nodes(1):
        # 3 expired + limit=3: window is empty, so all 3 new requests pass
        for _ in range(3):
            _check_rate_limit("k2", 3)  # must not raise


def test_mixed_fresh_and_expired_counts_only_fresh():
    old = time.monotonic() - 61.0
    # 2 expired, 1 fresh
    keys_module._rpm_windows["k3"] = collections.deque([old, old, time.monotonic()])
    with _patch_nodes(1):
        # 1 fresh + limit=2: one more should pass, then fail
        _check_rate_limit("k3", 2)  # 2nd fresh — still at limit, passes
        with pytest.raises(HTTPException):
            _check_rate_limit("k3", 2)  # 3rd fresh — exceeds limit=2


# ── cluster-aware division ────────────────────────────────────────────────────

def test_limit_divided_by_node_count():
    # 2 nodes, limit=4 → per-node limit = 2
    with _patch_nodes(2):
        _check_rate_limit("k4", 4)  # 1st OK
        _check_rate_limit("k4", 4)  # 2nd OK (per-node limit = 2)
        with pytest.raises(HTTPException):
            _check_rate_limit("k4", 4)  # 3rd exceeds per-node limit


def test_single_node_uses_full_limit():
    with _patch_nodes(1):
        for _ in range(5):
            _check_rate_limit("k5", 5)
        with pytest.raises(HTTPException):
            _check_rate_limit("k5", 5)


def test_many_nodes_enforces_minimum_per_node_limit_of_1():
    # 100 nodes, limit=3 → per_node = max(1, 3//100) = max(1, 0) = 1
    with _patch_nodes(100):
        _check_rate_limit("k6", 3)  # 1st — OK (limit=1 per node)
        with pytest.raises(HTTPException):
            _check_rate_limit("k6", 3)  # 2nd exceeds per-node limit of 1


# ── key isolation ─────────────────────────────────────────────────────────────

def test_different_keys_tracked_independently():
    with _patch_nodes(1):
        for _ in range(3):
            _check_rate_limit("key_a", 3)
        # key_b has its own separate window
        _check_rate_limit("key_b", 3)  # must not raise


def test_exhausted_key_does_not_block_other_key():
    with _patch_nodes(1):
        for _ in range(2):
            _check_rate_limit("exhausted", 2)
        with pytest.raises(HTTPException):
            _check_rate_limit("exhausted", 2)
        # Other key unaffected
        _check_rate_limit("fresh_key", 2)  # must not raise
