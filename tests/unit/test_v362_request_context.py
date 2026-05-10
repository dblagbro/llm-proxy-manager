"""v3.6.2 — request context (client IP capture) tests."""
from __future__ import annotations

import pytest

from app.observability.request_context import (
    extract_client_ip_from_request,
    get_client_ip,
    set_client_ip,
)


# ── Stub Request object ────────────────────────────────────────────


class _StubClient:
    def __init__(self, host: str):
        self.host = host


class _StubRequest:
    def __init__(self, headers=None, client_host=None):
        self.headers = headers or {}
        self.client = _StubClient(client_host) if client_host else None


# ── extract_client_ip_from_request ─────────────────────────────────


def test_extracts_xff_first_hop():
    """nginx prepends real IP to X-Forwarded-For."""
    req = _StubRequest(
        headers={"x-forwarded-for": "203.0.113.42"},
        client_host="172.18.0.5",
    )
    assert extract_client_ip_from_request(req) == "203.0.113.42"


def test_xff_multi_hop_takes_leftmost():
    """Real client at far-left of XFF chain, intermediate proxies after."""
    req = _StubRequest(
        headers={"x-forwarded-for": "203.0.113.42, 198.51.100.1, 172.18.0.5"},
        client_host="172.18.0.5",
    )
    assert extract_client_ip_from_request(req) == "203.0.113.42"


def test_xff_strips_whitespace():
    req = _StubRequest(headers={"x-forwarded-for": "  203.0.113.42  ,  198.51.100.1"})
    assert extract_client_ip_from_request(req) == "203.0.113.42"


def test_falls_back_to_x_real_ip():
    """Some reverse proxies use X-Real-IP instead of XFF."""
    req = _StubRequest(
        headers={"x-real-ip": "203.0.113.42"},
        client_host="172.18.0.5",
    )
    assert extract_client_ip_from_request(req) == "203.0.113.42"


def test_falls_back_to_socket_peer():
    """No proxy headers → use the raw client.host."""
    req = _StubRequest(client_host="192.168.1.50")
    assert extract_client_ip_from_request(req) == "192.168.1.50"


def test_xff_takes_priority_over_real_ip():
    req = _StubRequest(headers={
        "x-forwarded-for": "203.0.113.42",
        "x-real-ip": "198.51.100.99",
    })
    assert extract_client_ip_from_request(req) == "203.0.113.42"


def test_returns_none_when_no_signal():
    req = _StubRequest()
    assert extract_client_ip_from_request(req) is None


def test_handles_empty_xff_gracefully():
    """Empty XFF header (rare, but possible from broken proxies) →
    fall through to next signal, not return empty string."""
    req = _StubRequest(headers={"x-forwarded-for": ""}, client_host="192.168.1.50")
    assert extract_client_ip_from_request(req) == "192.168.1.50"


def test_handles_xff_with_only_whitespace():
    req = _StubRequest(headers={"x-forwarded-for": "   "}, client_host="192.168.1.50")
    assert extract_client_ip_from_request(req) == "192.168.1.50"


def test_handles_malformed_request_object_gracefully():
    """Defensive: any unexpected attribute access shouldn't 500."""
    class Bogus:
        pass
    out = extract_client_ip_from_request(Bogus())
    assert out is None


# ── set_client_ip / get_client_ip ──────────────────────────────────


def test_get_returns_none_outside_context():
    """Default contextvar value is None."""
    set_client_ip(None)  # ensure clean
    assert get_client_ip() is None


def test_set_then_get_round_trip():
    set_client_ip("203.0.113.99")
    assert get_client_ip() == "203.0.113.99"


def test_empty_string_clears_to_none():
    """Defensive: empty IP coerced to None so JSON omits the key."""
    set_client_ip("203.0.113.42")
    assert get_client_ip() == "203.0.113.42"
    set_client_ip("")
    assert get_client_ip() is None


# ── record_outcome wiring (regression) ─────────────────────────────


def test_record_outcome_meta_includes_client_ip_field_signature():
    """v3.6.2 must surface client_ip in the meta dict that goes to
    activity_log. Source-level check that the wiring is in place."""
    import inspect
    from app.monitoring import helpers
    src = inspect.getsource(helpers.record_outcome)
    # Both success and error paths read get_client_ip
    assert src.count("get_client_ip") >= 2
    # Both paths add api_key_id for joins
    assert src.count('"api_key_id"') >= 2
    # client_ip key only added when truthy
    assert 'meta["client_ip"]' in src
