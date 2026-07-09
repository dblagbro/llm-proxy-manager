"""v5.17.1 — keepalive gates on chronic CB re-open cycles.

Trigger: watching logs on 2026-07-02, found Grok-Web-Devin on the clone
cluster had generated 19 keepalive_probe events (10 warning + 9 error)
in 24h — CB was cycling open every ~2min per #513 (bridge timeouts on
/api/chat). No diagnostic value; pure activity_log noise.

Fix: gate keepalive probes when consecutive_opens crosses threshold
(default 5). Apply a 6h backoff. When the backoff window elapses, let
one probe fire to detect recovery.
"""
from __future__ import annotations
from pathlib import Path


def test_circuit_breaker_exposes_consecutive_opens():
    from app.routing.circuit_breaker import get_all_states, get_consecutive_opens
    # Callable
    assert callable(get_consecutive_opens)
    # Unknown provider → 0 (no probe has run yet)
    assert get_consecutive_opens("nonexistent-pid") == 0


def test_get_all_states_surfaces_consecutive_opens():
    """UI/health surface must see the same counter the keepalive worker
    reads so operators + auto-checks stay consistent."""
    src = Path("app/routing/circuit_breaker.py").read_text()
    assert '"consecutive_opens": s.consecutive_opens,' in src


def test_keepalive_reads_consecutive_opens_and_gates():
    # v5.19.1 — gate moved from sweep loop to _probe_one, so the local
    # variable name changed from `p.id` to `provider.id`. Same intent:
    # keepalive reads consecutive_opens and gates chronic-CB providers.
    src = Path("app/monitoring/keepalive.py").read_text()
    assert "from app.routing.circuit_breaker import get_consecutive_opens" in src
    assert (
        "get_consecutive_opens(p.id)" in src
        or "get_consecutive_opens(provider.id)" in src
    ), "keepalive must call get_consecutive_opens for gating"
    assert "keepalive.chronic_cb_gated" in src


def test_keepalive_settings_default_threshold_and_backoff():
    from app.config import settings
    assert getattr(settings, "keepalive_chronic_cb_open_threshold") == 5
    assert getattr(settings, "keepalive_chronic_cb_open_backoff_sec") == 21600


def test_backoff_cache_separate_from_probe_backoff():
    """The chronic-CB backoff cache MUST be a distinct dict — the
    existing _probe_backoff_until is used for rate-limit response
    backoffs and has different eviction semantics."""
    from app.monitoring import keepalive
    assert hasattr(keepalive, "_chronic_backoff_until")
    assert hasattr(keepalive, "_probe_backoff_until")
    assert keepalive._chronic_backoff_until is not keepalive._probe_backoff_until


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 17, 1), (
        f"expected >= 5.17.1, got {major}.{minor}.{patch}"
    )
