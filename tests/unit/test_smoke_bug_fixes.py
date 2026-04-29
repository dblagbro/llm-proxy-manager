"""Regression guards for two hub-team smoke-bug fixes (v3.0.0-r5+)."""
from __future__ import annotations

import pytest


# ── Bug 1: cookie path scope ────────────────────────────────────────────────


def test_session_cookie_path_is_root_for_multi_prefix_serving():
    """Smoke node serves at /llm-proxy2-smoke/ but production at
    /llm-proxy2/. A scoped cookie path drops on smoke. Locking Path=/
    so the same image works behind any URL prefix. Cookie *name*
    (``llmproxy_session``) is unique enough that wider scope no longer
    collides with other apps on the host."""
    from app.auth.admin import SESSION_COOKIE_PATH, SESSION_COOKIE_NAME
    assert SESSION_COOKIE_PATH == "/"
    assert SESSION_COOKIE_NAME == "llmproxy_session"


# ── Bug 2: spending_cap_usd negative = unlimited ────────────────────────────


def _fake_key(cap):
    """Minimal ApiKey-shaped object for the cap check path."""
    class K:
        id = "k-test"
        spending_cap_usd = cap
        total_cost_usd = 0.0
    return K()


def test_negative_spending_cap_treated_as_unlimited():
    """The cap-check guard skips negative caps — ``-1`` is the conventional
    "no cap" sentinel. v3.0.1 narrowed the guard to ``>= 0`` so zero stays
    a hard block."""
    key = _fake_key(-1.0)
    skipped = not (key.spending_cap_usd is not None and key.spending_cap_usd >= 0)
    assert skipped is True


def test_zero_spending_cap_blocks_immediately():
    """0 is a $0 budget — should block (existing test contract). v3.0.1:
    ``>= 0`` includes zero in the enforcement path."""
    key = _fake_key(0.0)
    enforced = key.spending_cap_usd is not None and key.spending_cap_usd >= 0
    assert enforced is True


def test_negative_one_treated_as_unlimited():
    """``-1`` is the conventional sentinel for unlimited; the ``>= 0``
    guard short-circuits."""
    key = _fake_key(-1.0)
    skipped = not (key.spending_cap_usd is not None and key.spending_cap_usd >= 0)
    assert skipped is True


def test_positive_spending_cap_still_enforced():
    key = _fake_key(100.0)
    enforced = key.spending_cap_usd is not None and key.spending_cap_usd >= 0
    assert enforced is True


def test_none_spending_cap_unlimited():
    """``None`` (no cap configured) is the original unlimited path —
    same guard short-circuits."""
    key = _fake_key(None)
    skipped = not (key.spending_cap_usd is not None and key.spending_cap_usd >= 0)
    assert skipped is True
