"""v3.6.3 — LAN-egress IP rewrite tests."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.observability.request_context import (
    _clear_dns_cache_for_tests,
    _resolve_cached,
    _maybe_rewrite_lan_ip,
    set_client_ip,
    get_client_ip,
    get_client_ip_inside,
    prewarm_lan_egress_dns,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    _clear_dns_cache_for_tests()
    set_client_ip(None)
    yield
    _clear_dns_cache_for_tests()
    set_client_ip(None)


# ── _resolve_cached ────────────────────────────────────────────────


def test_resolve_returns_ip_for_known_hostname():
    """gethostbyname is mocked to return a known IP."""
    with patch("socket.gethostbyname", return_value="24.168.14.36"):
        assert _resolve_cached("ip.voipguru.org") == "24.168.14.36"


def test_resolve_caches_within_ttl():
    """Second call within TTL doesn't re-resolve."""
    with patch("socket.gethostbyname", return_value="24.168.14.36") as m:
        _resolve_cached("ip.voipguru.org")
        _resolve_cached("ip.voipguru.org")
        _resolve_cached("ip.voipguru.org")
    assert m.call_count == 1


def test_resolve_caches_failures_too():
    """A NXDOMAIN result is cached so we don't burn DNS on every request."""
    with patch("socket.gethostbyname", side_effect=Exception("NXDOMAIN")) as m:
        a = _resolve_cached("nonexistent.example")
        b = _resolve_cached("nonexistent.example")
    assert a is None and b is None
    assert m.call_count == 1


def test_resolve_handles_empty_hostname():
    assert _resolve_cached("") is None
    assert _resolve_cached(None) is None  # type: ignore[arg-type]


# ── _maybe_rewrite_lan_ip ──────────────────────────────────────────


def test_no_rewrite_when_map_empty():
    """No setting → never rewrites."""
    from app.config import settings
    settings.client_ip_lan_resolve_map = {}
    assert _maybe_rewrite_lan_ip("192.168.18.1") is None


def test_rewrites_known_lan_gateway():
    from app.config import settings
    settings.client_ip_lan_resolve_map = {"192.168.18.1": "ip.voipguru.org"}
    with patch("socket.gethostbyname", return_value="24.168.14.36"):
        assert _maybe_rewrite_lan_ip("192.168.18.1") == "24.168.14.36"


def test_no_rewrite_for_unknown_ip():
    from app.config import settings
    settings.client_ip_lan_resolve_map = {"192.168.18.1": "ip.voipguru.org"}
    with patch("socket.gethostbyname", return_value="24.168.14.36"):
        assert _maybe_rewrite_lan_ip("203.0.113.99") is None


def test_no_rewrite_when_dns_fails():
    """Resolver returns None → caller falls back to inside IP."""
    from app.config import settings
    settings.client_ip_lan_resolve_map = {"192.168.18.1": "ip.voipguru.org"}
    with patch("socket.gethostbyname", side_effect=Exception("dns down")):
        assert _maybe_rewrite_lan_ip("192.168.18.1") is None


# ── set_client_ip with rewrite ─────────────────────────────────────


def test_set_records_both_public_and_inside_when_rewriting():
    from app.config import settings
    settings.client_ip_lan_resolve_map = {"192.168.18.1": "ip.voipguru.org"}
    with patch("socket.gethostbyname", return_value="24.168.14.36"):
        set_client_ip("192.168.18.1")
    assert get_client_ip() == "24.168.14.36"
    assert get_client_ip_inside() == "192.168.18.1"


def test_set_falls_back_to_inside_when_dns_fails():
    """If the configured hostname doesn't resolve, fall back to the
    raw inside IP rather than logging None."""
    from app.config import settings
    settings.client_ip_lan_resolve_map = {"192.168.18.1": "ip.voipguru.org"}
    with patch("socket.gethostbyname", side_effect=Exception("dns down")):
        set_client_ip("192.168.18.1")
    # Both should be the inside IP (no rewrite happened)
    assert get_client_ip() == "192.168.18.1"
    assert get_client_ip_inside() == "192.168.18.1"


def test_set_passes_through_non_lan_ips():
    """A real public IP that isn't in the rewrite map → no transformation."""
    from app.config import settings
    settings.client_ip_lan_resolve_map = {"192.168.18.1": "ip.voipguru.org"}
    set_client_ip("198.51.100.42")
    assert get_client_ip() == "198.51.100.42"
    assert get_client_ip_inside() == "198.51.100.42"


def test_set_none_clears_both():
    set_client_ip("192.168.18.1")
    set_client_ip(None)
    assert get_client_ip() is None
    assert get_client_ip_inside() is None


# ── prewarm_lan_egress_dns ─────────────────────────────────────────


def test_prewarm_resolves_each_configured_hostname():
    from app.config import settings
    settings.client_ip_lan_resolve_map = {
        "192.168.18.1": "ip.voipguru.org",
        "10.0.0.1": "another.example",
    }
    with patch("socket.gethostbyname", return_value="1.2.3.4") as m:
        prewarm_lan_egress_dns()
    # Two distinct hostnames → two resolves
    assert m.call_count == 2


def test_prewarm_handles_empty_map():
    from app.config import settings
    settings.client_ip_lan_resolve_map = {}
    with patch("socket.gethostbyname") as m:
        prewarm_lan_egress_dns()
    assert m.call_count == 0


def test_prewarm_doesnt_crash_on_dns_error():
    """Defensive: a misconfigured hostname must not break startup."""
    from app.config import settings
    settings.client_ip_lan_resolve_map = {"192.168.18.1": "bogus.example"}
    with patch("socket.gethostbyname", side_effect=Exception("dns down")):
        prewarm_lan_egress_dns()  # no raise


# ── record_outcome wiring ──────────────────────────────────────────


def test_record_outcome_emits_client_ip_inside_only_when_different():
    """Source-level check that the meta dict only carries
    ``client_ip_inside`` when the rewrite actually happened — no
    point doubling the storage cost on rows where the IP is the same."""
    import inspect
    from app.monitoring import helpers
    src = inspect.getsource(helpers.record_outcome)
    # Both success and error paths should reference get_client_ip_inside
    assert src.count("get_client_ip_inside") >= 2
    # The "only emit when different" guard
    assert "client_ip_inside != client_ip" in src
