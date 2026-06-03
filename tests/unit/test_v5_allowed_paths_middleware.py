"""v5.0.0 — allowed_paths middleware (spec §8.3)."""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})


from app.middleware.allowed_paths import (  # noqa: E402
    _normalize_path,
    allowed_paths_middleware,
)


def _stub_request(path: str, *, key_header="bearer llmp-test"):
    req = MagicMock()
    req.headers = {"authorization": key_header, "user-agent": "curl/8"}
    req.url = MagicMock()
    req.url.path = path
    return req


def _stub_key(*, kid="key-1", allowed_paths=None, debug_echo=False):
    k = MagicMock()
    k.id = kid
    k.allowed_paths = allowed_paths
    k.debug_echo_enabled = debug_echo
    return k


# ── _normalize_path ─────────────────────────────────────────────────


def test_normalize_strips_proxy_prefix():
    assert _normalize_path("/llm-proxy2/v1/messages") == "/v1/messages"
    assert _normalize_path("/llm-proxy2-smoke/v1/messages") == "/v1/messages"
    assert _normalize_path("/v1/messages") == "/v1/messages"


def test_normalize_strips_trailing_slash():
    assert _normalize_path("/v1/messages/") == "/v1/messages"
    assert _normalize_path("/") == "/"


def test_normalize_collapses_double_slash():
    assert _normalize_path("/v1//messages") == "/v1/messages"


# ── Pass-through cases ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_passthrough_when_allowed_paths_is_none():
    req = _stub_request("/v1/embeddings")
    call_next = AsyncMock(return_value=MagicMock(status_code=200))
    with patch(
        "app.middleware.allowed_paths._resolve_api_key",
        new=AsyncMock(return_value=_stub_key(allowed_paths=None)),
    ):
        await allowed_paths_middleware(req, call_next)
    call_next.assert_awaited_once()


@pytest.mark.asyncio
async def test_passthrough_when_no_api_key():
    """No api key on the request → auth layer handles it. We pass through."""
    req = MagicMock()
    req.headers = {}
    req.url = MagicMock()
    req.url.path = "/v1/embeddings"
    call_next = AsyncMock(return_value=MagicMock(status_code=200))
    await allowed_paths_middleware(req, call_next)
    call_next.assert_awaited_once()


@pytest.mark.asyncio
async def test_passthrough_when_path_in_allowed_list():
    req = _stub_request("/v1/chat/completions")
    call_next = AsyncMock(return_value=MagicMock(status_code=200))
    with patch(
        "app.middleware.allowed_paths._resolve_api_key",
        new=AsyncMock(return_value=_stub_key(
            allowed_paths=["/v1/chat/completions", "/v1/models", "/health"],
        )),
    ):
        await allowed_paths_middleware(req, call_next)
    call_next.assert_awaited_once()


# ── 403 + audit row ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_403_when_path_not_in_allowed_list():
    req = _stub_request("/v1/embeddings")
    call_next = AsyncMock()
    with patch(
        "app.middleware.allowed_paths._resolve_api_key",
        new=AsyncMock(return_value=_stub_key(
            allowed_paths=["/v1/chat/completions", "/v1/models", "/health"],
        )),
    ), patch(
        "app.middleware.allowed_paths._emit_path_block_event",
        new=AsyncMock(return_value="comp_test"),
    ) as emit:
        result = await allowed_paths_middleware(req, call_next)
    call_next.assert_not_awaited()
    assert result.status_code == 403
    assert result.headers.get("X-Compliance-Reason") == "path-not-in-allowed_paths"
    assert result.headers.get("X-Compliance-Audit-Id") == "comp_test"
    emit.assert_awaited_once()


# ── Debug-echo bypass ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_debug_echo_bypass_when_enabled():
    req = _stub_request("/api/debug/echo-client")
    call_next = AsyncMock(return_value=MagicMock(status_code=200))
    with patch(
        "app.middleware.allowed_paths._resolve_api_key",
        new=AsyncMock(return_value=_stub_key(
            allowed_paths=["/v1/chat/completions"],
            debug_echo=True,
        )),
    ):
        await allowed_paths_middleware(req, call_next)
    call_next.assert_awaited_once()


@pytest.mark.asyncio
async def test_debug_echo_blocked_when_not_enabled():
    """Key without debug_echo_enabled still gets 403 on /api/debug/echo-client
    when it isn't in allowed_paths."""
    req = _stub_request("/api/debug/echo-client")
    call_next = AsyncMock()
    with patch(
        "app.middleware.allowed_paths._resolve_api_key",
        new=AsyncMock(return_value=_stub_key(
            allowed_paths=["/v1/chat/completions"],
            debug_echo=False,
        )),
    ), patch(
        "app.middleware.allowed_paths._emit_path_block_event",
        new=AsyncMock(return_value="comp_test"),
    ):
        result = await allowed_paths_middleware(req, call_next)
    call_next.assert_not_awaited()
    assert result.status_code == 403


# ── Fail-open on exception ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_fail_open_on_db_error():
    req = _stub_request("/v1/embeddings")
    call_next = AsyncMock(return_value=MagicMock(status_code=200))
    with patch(
        "app.middleware.allowed_paths._resolve_api_key",
        new=AsyncMock(side_effect=Exception("DB down")),
    ):
        await allowed_paths_middleware(req, call_next)
    call_next.assert_awaited_once()


# ── Wiring ──────────────────────────────────────────────────────────


def test_middleware_registered_after_ip_block():
    """Ordering check: ip_block first (outermost), allowed_paths second."""
    from pathlib import Path
    src = Path("app/main.py").read_text()
    ip_idx = src.index("app.middleware(\"http\")(ip_block_middleware)")
    ap_idx = src.index("app.middleware(\"http\")(allowed_paths_middleware)")
    assert ip_idx < ap_idx
