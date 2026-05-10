"""v3.7.11 — IP block middleware tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.middleware.ip_block import (
    _clear_cache_for_tests,
    _load_blocked_set,
    is_blocked,
    ip_block_middleware,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


# ── _load_blocked_set ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_blocked_set_returns_frozenset():
    """Empty DB → empty frozenset (not None or list).

    Patch the source module (``app.models.database``) since
    ``_load_blocked_set`` does the import inline.
    """
    with patch("app.models.database.AsyncSessionLocal") as MockSession:
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        rs = MagicMock()
        rs.all = MagicMock(return_value=[])
        session.execute = AsyncMock(return_value=rs)
        MockSession.return_value = session
        result = await _load_blocked_set()
    assert isinstance(result, frozenset)
    assert len(result) == 0


@pytest.mark.asyncio
async def test_load_blocked_set_fails_open():
    """DB error → empty frozenset (fail open, don't block all traffic)."""
    with patch("app.models.database.AsyncSessionLocal", side_effect=Exception("DB down")):
        result = await _load_blocked_set()
    assert isinstance(result, frozenset)
    assert len(result) == 0


# ── is_blocked ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_blocked_none_returns_false():
    assert (await is_blocked(None)) is False
    assert (await is_blocked("")) is False


@pytest.mark.asyncio
async def test_is_blocked_unknown_ip_returns_false():
    with patch("app.middleware.ip_block._load_blocked_set", new=AsyncMock(return_value=frozenset(["1.2.3.4"]))):
        result = await is_blocked("5.6.7.8")
    assert result is False


@pytest.mark.asyncio
async def test_is_blocked_known_ip_returns_true():
    with patch("app.middleware.ip_block._load_blocked_set", new=AsyncMock(return_value=frozenset(["1.2.3.4"]))):
        result = await is_blocked("1.2.3.4")
    assert result is True


# ── ip_block_middleware ───────────────────────────────────────────


def _stub_request(headers=None, client_host=None):
    req = MagicMock()
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = client_host
    req.url = MagicMock()
    req.url.path = "/v1/messages"
    return req


@pytest.mark.asyncio
async def test_middleware_allows_unknown_ip():
    """No IP, or non-blocked IP → call_next is invoked."""
    req = _stub_request(headers={"x-forwarded-for": "203.0.113.42"})
    call_next = AsyncMock(return_value=MagicMock(status_code=200))
    with patch("app.middleware.ip_block.is_blocked", new=AsyncMock(return_value=False)):
        result = await ip_block_middleware(req, call_next)
    call_next.assert_awaited_once()
    assert result is not None


@pytest.mark.asyncio
async def test_middleware_rejects_blocked_ip_with_403():
    """Blocked IP → 403 returned, call_next NOT invoked."""
    req = _stub_request(headers={"x-forwarded-for": "203.0.113.42"})
    call_next = AsyncMock()
    with patch("app.middleware.ip_block.is_blocked", new=AsyncMock(return_value=True)):
        result = await ip_block_middleware(req, call_next)
    call_next.assert_not_awaited()
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_middleware_checks_rewritten_ip():
    """LAN-NAT'd traffic: raw IP not blocked, but rewritten public IP is."""
    req = _stub_request(headers={"x-forwarded-for": "192.168.18.1"})
    call_next = AsyncMock()
    # is_blocked returns False for "192.168.18.1", True for "203.0.113.99"
    async def fake_blocked(ip):
        return ip == "203.0.113.99"
    with patch("app.middleware.ip_block.is_blocked", new=fake_blocked), \
         patch("app.observability.request_context._maybe_rewrite_lan_ip", return_value="203.0.113.99"):
        result = await ip_block_middleware(req, call_next)
    call_next.assert_not_awaited()
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_middleware_fails_open_on_check_error():
    """If is_blocked raises, fail OPEN (don't 500/403 the request)."""
    req = _stub_request(headers={"x-forwarded-for": "203.0.113.42"})
    call_next = AsyncMock(return_value=MagicMock(status_code=200))
    with patch("app.middleware.ip_block.is_blocked", side_effect=Exception("oops")):
        result = await ip_block_middleware(req, call_next)
    call_next.assert_awaited_once()


# ── Wiring regression ─────────────────────────────────────────────


def test_blocked_ip_model_exists():
    from app.models.db import BlockedIp
    cols = {c.name for c in BlockedIp.__table__.columns}
    assert {"ip", "reason", "added_at", "added_by"}.issubset(cols)


def test_admin_router_registered():
    from pathlib import Path
    src = Path("app/main.py").read_text()
    assert "blocked_ips_router" in src
    assert "include_router(blocked_ips_router)" in src


def test_middleware_registered_first_for_outermost_position():
    """The IP block middleware must be the FIRST middleware registered
    so it wraps everything else (outermost in the ASGI stack = first
    to see incoming request)."""
    from pathlib import Path
    src = Path("app/main.py").read_text()
    # The middleware registration line must appear BEFORE the
    # log_requests decorator.
    ip_block_idx = src.index("ip_block_middleware")
    log_requests_idx = src.index("async def log_requests")
    assert ip_block_idx < log_requests_idx


def test_admin_endpoints_use_require_admin():
    """All blocked-ip admin endpoints must be admin-gated."""
    from pathlib import Path
    src = Path("app/api/blocked_ips.py").read_text()
    assert "require_admin" in src
    # All three endpoint handlers should depend on require_admin
    assert src.count("require_admin") >= 3


def test_admin_endpoint_paths():
    """Endpoint paths must match the documented contract."""
    from pathlib import Path
    src = Path("app/api/blocked_ips.py").read_text()
    assert '@router.get("/blocked-ips")' in src
    assert '@router.post("/blocked-ips")' in src
    assert '@router.delete("/blocked-ips/' in src
