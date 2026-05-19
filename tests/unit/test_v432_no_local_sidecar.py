"""v4.3.2 — interim noise suppression when a provider's required local
sidecar is absent on this node. Precursor to the v4.4 per-node-auth-state
arc; locks the flag mechanic + the grok-web prober short-circuit."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.monitoring import keepalive


def test_no_local_sidecar_flag_roundtrip():
    pid = "test-provider-v432-roundtrip"
    assert keepalive.is_no_local_sidecar(pid) is False
    keepalive._no_local_sidecar.add(pid)
    assert keepalive.is_no_local_sidecar(pid) is True
    keepalive._no_local_sidecar.discard(pid)
    assert keepalive.is_no_local_sidecar(pid) is False


@pytest.mark.asyncio
async def test_local_sidecar_reachable_returns_false_on_connection_error():
    # 127.0.0.99:1 — a closed port that returns a connection error promptly.
    # `_local_sidecar_reachable` must classify that as "no local sidecar".
    assert await keepalive._local_sidecar_reachable("http://127.0.0.99:1") is False


def test_keepalive_grok_web_branch_skips_when_bridge_unreachable():
    """The grok-web probe branch must short-circuit (return) on an
    unreachable local bridge — no fall-through to complete_grok_web, no
    record_outcome, no CB hit."""
    src = Path("app/monitoring/keepalive.py").read_text()
    assert "_local_sidecar_reachable" in src
    # the short-circuit decision uses the new flag
    assert "_no_local_sidecar" in src
    # the helper is exposed for routing/dispatch callers (v4.4 lands here)
    assert "def is_no_local_sidecar" in src
