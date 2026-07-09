"""v5.8.7 — regression test for the keepalive sweep gate that skips
auto-skipped providers.

Pre-v5.8.7, the keepalive sweep checked the CB hold-down via
``is_available`` but not the provider's ``auto_skip_until``. Since the
CB's 120s hold-down typically expires BEFORE the next sweep, an
auto-skipped provider got re-probed every cycle → 401 → CB.opened with
failures+1 → one warning per provider per cycle indefinitely.
"""
from __future__ import annotations

import inspect


def test_keepalive_sweep_consults_auto_skip_until():
    from app.monitoring import keepalive as mod
    src = inspect.getsource(mod._probe_all_once)
    assert "auto_skip_until" in src, (
        "v5.8.7 introduced an auto_skip_until check in the keepalive "
        "sweep; the symbol must be present in _probe_all_once."
    )
    assert "keepalive.skipped_auto_skip" in src, (
        "the auto-skipped branch must log keepalive.skipped_auto_skip "
        "so the existing skip-reason taxonomy stays consistent with "
        "skipped_breaker_open / skipped_per_call / skipped_rate_limit_backoff."
    )


def test_keepalive_sweep_still_uses_is_available():
    """v5.8.7 must NOT regress the CB hold-down check from v3.0.49."""
    from app.monitoring import keepalive as mod
    src = inspect.getsource(mod._probe_all_once)
    assert "is_available(p.id)" in src
    assert "keepalive.skipped_breaker_open" in src
