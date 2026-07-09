"""v5.9.2 — regression test that the keepalive sweep checks
auto_skip_until BEFORE is_available.

v5.8.7 added the auto_skip_until gate AFTER the is_available CB check.
CB's 120s hold-down expires regularly, and the keepalive sweep aligns
close enough to that window that probes slip through → fail →
record_failure resets hold_down → cycle. Moving the auto_skip_until
check first makes it strictly dominant over CB hysteresis.
"""
from __future__ import annotations

import inspect


def test_auto_skip_check_appears_before_is_available_in_source():
    from app.monitoring import keepalive as mod
    src = inspect.getsource(mod._probe_all_once)
    # Both markers should exist
    assert "auto_skip_until" in src, "auto_skip_until gate missing"
    assert "is_available(p.id)" in src, "CB is_available check missing"
    # auto_skip_until check must appear FIRST.
    idx_auto = src.find("auto_skip_until")
    idx_avail = src.find("is_available(p.id)")
    assert 0 <= idx_auto < idx_avail, (
        f"v5.9.2 requires auto_skip_until check (idx {idx_auto}) to come "
        f"BEFORE is_available check (idx {idx_avail}) so the 'operator "
        "must re-auth' signal strictly dominates CB hysteresis."
    )
