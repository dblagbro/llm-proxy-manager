"""v3.10.15 — BUG-032 (infra-error observability) + BUG-036 (behavioral
coverage of the claude-oauth dispatch chain in _messages_dispatch.py)."""
from __future__ import annotations

import logging
import types

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

import app.api._messages_dispatch as disp


# ════════════════════════════════════════════════════════════════════════
# BUG-032 — infra-error tap classification + counter
# ════════════════════════════════════════════════════════════════════════

def test_bug032_classify_disconnect_vs_fault():
    from app.observability.infra_error_tap import classify_fault
    # benign client-side disconnects
    assert classify_fault("asyncio.CancelledError raised in send") == "disconnect"
    assert classify_fault("ConnectionResetError: [Errno 104]") == "disconnect"
    assert classify_fault("aiosqlite ValueError: Connection closed") == "disconnect"
    assert classify_fault("BrokenPipeError [Errno 32] broken pipe") == "disconnect"
    # genuine server-side / pool faults
    assert classify_fault(
        "The garbage collector is trying to clean up non-checked-in connection"
    ) == "fault"
    assert classify_fault("Exception terminating connection") == "fault"
    assert classify_fault("KeyError in ASGI application") == "fault"


def test_bug032_tap_calls_observe_with_classification(monkeypatch):
    """The tap classifies each record and forwards (source, fault_class)
    to observe_infra_error. Spy on observe_infra_error rather than the
    global counter — the real installed tap can fire concurrently (a
    background connection GC), which would make a counter-delta flaky."""
    from app.observability.infra_error_tap import _InfraErrorTap
    from app.observability import prometheus

    calls: list[tuple] = []
    monkeypatch.setattr(prometheus, "observe_infra_error",
                        lambda source, fc: calls.append((source, fc)))

    tap = _InfraErrorTap("pool", logging.WARNING)
    tap.emit(logging.LogRecord("sqlalchemy.pool", logging.ERROR, "x", 0,
                               "Exception terminating connection", None, None))
    tap.emit(logging.LogRecord("sqlalchemy.pool", logging.ERROR, "x", 0,
                               "ConnectionResetError during checkout", None, None))

    assert calls == [("pool", "fault"), ("pool", "disconnect")]


def test_bug032_tap_never_raises_on_bad_record():
    """A logging handler must not raise — even on a malformed record."""
    from app.observability.infra_error_tap import _InfraErrorTap
    tap = _InfraErrorTap("asgi", logging.ERROR)
    bad = logging.LogRecord("uvicorn.error", logging.ERROR, "x", 0,
                            "%s", ("only-one-but-format-wants-args",), None)
    tap.emit(bad)  # must not raise


# ════════════════════════════════════════════════════════════════════════
# BUG-036 — behavioral coverage of dispatch_claude_oauth_chain
# ════════════════════════════════════════════════════════════════════════

def _route(ptype, pid="p1", name="P1", api_key="tok"):
    prov = types.SimpleNamespace(
        provider_type=ptype, id=pid, name=name, api_key=api_key,
    )
    return types.SimpleNamespace(provider=prov)


def _key():
    return types.SimpleNamespace(id="key1", key_type="standard")


def _http_status_error(status: int, text: str) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://upstream/v1/messages")
    resp = httpx.Response(status, text=text, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


@pytest.fixture
def patched(monkeypatch):
    """Neutralise the cache / disclosure / memory collaborators so the
    tests exercise the dispatch *chain-walk logic* in isolation."""
    monkeypatch.setattr(disp, "parse_cache_mode",
                        lambda h: types.SimpleNamespace(mode="none"))
    monkeypatch.setattr(disp, "build_cache_disclosure", lambda **k: {})
    monkeypatch.setattr(disp, "append_cache_disclosure", lambda h, d: None)
    monkeypatch.setattr(disp, "merge_into_headers",
                        lambda h, r, endpoint=None: None)

    async def _noop_extract(*a, **k):
        return 0
    monkeypatch.setattr(disp, "maybe_extract_memory_writes", _noop_extract)
    monkeypatch.setattr(disp.settings, "fallback_enabled", True)
    return monkeypatch


async def _dispatch(route, **over):
    kw = dict(
        body={}, db=None, key_record=_key(), resp_headers={},
        stream=False, max_tokens=100, llm_hint=None, hint=None,
        has_tools=False, has_images=False, conversation_id=None, memory_tag=None,
    )
    kw.update(over)
    return await disp.dispatch_claude_oauth_chain(route, **kw)


@pytest.mark.asyncio
async def test_non_oauth_route_falls_through(patched):
    route = _route("openai")
    resp, out_route = await _dispatch(route)
    assert resp is None and out_route is route


@pytest.mark.asyncio
async def test_oauth_success_returns_jsonresponse(patched):
    async def fake_complete(*a, **k):
        return {"content": [{"type": "text", "text": "hi"}], "usage": {}}
    patched.setattr(disp, "_complete_claude_oauth", fake_complete)
    resp, _ = await _dispatch(_route("claude-oauth"))
    assert isinstance(resp, JSONResponse)


@pytest.mark.asyncio
async def test_oauth_401_falls_over_to_next_provider(patched):
    next_route = _route("openai", pid="p2")

    async def fake_complete(*a, **k):
        raise _http_status_error(401, "invalid x-api-key")

    async def fake_select_excluding(*a, **k):
        return next_route

    patched.setattr(disp, "_complete_claude_oauth", fake_complete)
    patched.setattr(disp, "_select_excluding", fake_select_excluding)
    resp, out_route = await _dispatch(_route("claude-oauth"))
    # fell through the chain to a non-oauth provider for the litellm path
    assert resp is None and out_route is next_route


@pytest.mark.asyncio
async def test_oauth_network_error_falls_over(patched):
    next_route = _route("openai", pid="p2")

    async def fake_complete(*a, **k):
        raise httpx.ConnectError("connection refused")

    async def fake_select_excluding(*a, **k):
        return next_route

    patched.setattr(disp, "_complete_claude_oauth", fake_complete)
    patched.setattr(disp, "_select_excluding", fake_select_excluding)
    resp, out_route = await _dispatch(_route("claude-oauth"))
    assert resp is None and out_route is next_route


@pytest.mark.asyncio
async def test_oauth_fallback_exhausted_raises_httpexception(patched):
    async def fake_complete(*a, **k):
        raise _http_status_error(403, "forbidden")

    async def fake_select_excluding(*a, **k):
        raise RuntimeError("All providers tried")

    patched.setattr(disp, "_complete_claude_oauth", fake_complete)
    patched.setattr(disp, "_select_excluding", fake_select_excluding)
    with pytest.raises(HTTPException) as exc:
        await _dispatch(_route("claude-oauth"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_streaming_preflight_http_error_raises(patched):
    async def fake_stream(*a, **k):
        raise _http_status_error(429, "rate limited")
        yield b""  # pragma: no cover — makes fake_stream an async generator

    patched.setattr(disp, "_stream_claude_oauth", fake_stream)
    with pytest.raises(HTTPException) as exc:
        await _dispatch(_route("claude-oauth"), stream=True)
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_streaming_empty_raises_502(patched):
    async def fake_stream(*a, **k):
        return
        yield b""  # pragma: no cover — makes fake_stream an async generator

    patched.setattr(disp, "_stream_claude_oauth", fake_stream)
    with pytest.raises(HTTPException) as exc:
        await _dispatch(_route("claude-oauth"), stream=True)
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_streaming_success_returns_streamingresponse(patched):
    async def fake_stream(*a, **k):
        yield b'data: {"type":"message_start"}\n\n'
        yield b'data: {"type":"message_stop"}\n\n'

    patched.setattr(disp, "_stream_claude_oauth", fake_stream)
    resp, _ = await _dispatch(_route("claude-oauth"), stream=True)
    assert isinstance(resp, StreamingResponse)
