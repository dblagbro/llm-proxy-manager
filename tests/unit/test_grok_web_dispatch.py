"""Unit tests for the v3.2.9 shared grok-web dispatch helpers.

``app/api/_grok_web_dispatch.py`` was extracted from the duplicated
~50-line blocks in messages.py + completions.py. Tests here verify:

- The OpenAI-shape dispatcher returns JSONResponse on success and
  raises HTTPException with the right status on errors.
- The Anthropic-shape dispatcher translates OpenAI shape → Anthropic
  shape and handles ``system`` (string OR list-of-blocks).
- Streaming returns StreamingResponse and the first-chunk preflight
  surfaces auth errors as 401 before stream-start (so callers don't
  see SSE-error-then-200).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import AsyncIterator

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.api._grok_web_dispatch import (
    _flatten_anthropic_system,
    dispatch_grok_web_anthropic,
    dispatch_grok_web_openai,
)


def _route(provider_extra_config: dict | None = None,
           default_model: str = "grok-3"):
    """Build a minimal RouteResult-shaped object for the dispatchers."""
    return SimpleNamespace(
        provider=SimpleNamespace(extra_config=provider_extra_config or {}),
        profile=SimpleNamespace(model_id=default_model),
    )


# ── system flattening (Anthropic quirk) ─────────────────────────────────


def test_flatten_anthropic_system_string():
    assert _flatten_anthropic_system("you are helpful") == "you are helpful"


def test_flatten_anthropic_system_list_of_blocks():
    """Anthropic /v1/messages accepts ``system: [{type:text,text:...}]``.
    grok.com only takes a flat string, so we concatenate."""
    blocks = [
        {"type": "text", "text": "Part one."},
        {"type": "text", "text": "Part two."},
    ]
    assert _flatten_anthropic_system(blocks) == "Part one.\nPart two."


def test_flatten_anthropic_system_empty():
    assert _flatten_anthropic_system(None) is None
    assert _flatten_anthropic_system("") is None


# ── OpenAI-shape dispatch ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_dispatch_non_stream_returns_json(monkeypatch):
    """Happy path: complete_grok_web returns OpenAI dict; helper wraps
    in JSONResponse with the resp_headers we passed in."""
    captured = {}

    async def fake_complete(extra_config, *, messages, model, **kw):
        captured["model"] = model
        captured["messages"] = messages
        return {
            "id": "chatcmpl-x",
            "model": "grok-3",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr("app.providers.grok_web.complete_grok_web", fake_complete)

    headers = {"X-Test": "v"}
    out = await dispatch_grok_web_openai(
        route=_route({"bridge_url": "http://b", "conversation_id": "c"}),
        body={"model": "grok-3", "messages": [{"role": "user", "content": "ping"}]},
        stream=False,
        resp_headers=headers,
    )
    assert isinstance(out, JSONResponse)
    assert headers["X-Cache-Status"] == "bypass"  # always set on grok-web
    assert captured["model"] == "grok-3"
    assert captured["messages"] == [{"role": "user", "content": "ping"}]


@pytest.mark.asyncio
async def test_openai_dispatch_uses_route_default_when_body_lacks_model(monkeypatch):
    """When caller doesn't specify model, the routed provider's default
    model wins. Pre-extraction this was a copy-paste bug magnet."""
    captured = {}

    async def fake_complete(extra_config, *, messages, model, **kw):
        captured["model"] = model
        return {"id": "x", "model": model, "choices": [{"index": 0, "message": {"content": ""}}]}

    monkeypatch.setattr("app.providers.grok_web.complete_grok_web", fake_complete)

    await dispatch_grok_web_openai(
        route=_route({"bridge_url": "http://b", "conversation_id": "c"},
                     default_model="grok-4"),
        body={"messages": [{"role": "user", "content": "x"}]},  # no "model"
        stream=False,
        resp_headers={},
    )
    assert captured["model"] == "grok-4"


@pytest.mark.asyncio
async def test_openai_dispatch_auth_error_raises_401(monkeypatch):
    """GrokWebAuthError must surface as HTTP 401, not 502 — operator
    cookies are stale, prompt them to reauth instead of looking like
    upstream is broken."""
    from app.providers.grok_web import GrokWebAuthError

    async def fake_complete(*a, **kw):
        raise GrokWebAuthError("cookies expired")

    monkeypatch.setattr("app.providers.grok_web.complete_grok_web", fake_complete)

    with pytest.raises(HTTPException) as ex:
        await dispatch_grok_web_openai(
            route=_route({"bridge_url": "http://b", "conversation_id": "c"}),
            body={"model": "grok-3", "messages": [{"role": "user", "content": "x"}]},
            stream=False,
            resp_headers={},
        )
    assert ex.value.status_code == 401


@pytest.mark.asyncio
async def test_openai_dispatch_streaming_returns_streaming_response(monkeypatch):
    """Streaming preflight: dispatcher must consume the first chunk
    before returning so authentication errors raise as HTTPException
    instead of SSE-error-then-HTTP-200 (the v2.7.6 BUG-018 pattern)."""
    async def fake_stream(extra_config, *, messages, model, **kw):
        yield b"data: chunk-1\n\n"
        yield b"data: chunk-2\n\n"
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr("app.providers.grok_web.stream_grok_web", fake_stream)

    out = await dispatch_grok_web_openai(
        route=_route({"bridge_url": "http://b", "conversation_id": "c"}),
        body={"model": "grok-3", "messages": [{"role": "user", "content": "x"}],
              "stream": True},
        stream=True,
        resp_headers={},
    )
    assert isinstance(out, StreamingResponse)
    assert out.media_type == "text/event-stream"


@pytest.mark.asyncio
async def test_openai_dispatch_streaming_preflight_catches_auth(monkeypatch):
    """Auth error on first chunk must raise HTTP 401 BEFORE we hand
    back a StreamingResponse. Otherwise client sees a 200 + a stream
    that errors mid-flight, which is hard to handle."""
    from app.providers.grok_web import GrokWebAuthError

    async def fake_stream(*a, **kw):
        raise GrokWebAuthError("cookies expired")
        yield  # unreachable

    monkeypatch.setattr("app.providers.grok_web.stream_grok_web", fake_stream)

    with pytest.raises(HTTPException) as ex:
        await dispatch_grok_web_openai(
            route=_route({"bridge_url": "http://b", "conversation_id": "c"}),
            body={"model": "grok-3", "messages": [{"role": "user", "content": "x"}]},
            stream=True,
            resp_headers={},
        )
    assert ex.value.status_code == 401


# ── Anthropic-shape dispatch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_dispatch_translates_to_message_shape(monkeypatch):
    """Caller posted /v1/messages; we still ran complete_grok_web (which
    returns OpenAI shape), then translated to Anthropic /v1/messages
    shape. Verify the translation lands."""
    async def fake_complete(extra_config, *, messages, model, **kw):
        return {
            "id": "chatcmpl-y",
            "model": "grok-4",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }

    monkeypatch.setattr("app.providers.grok_web.complete_grok_web", fake_complete)

    out = await dispatch_grok_web_anthropic(
        route=_route({"bridge_url": "http://b", "conversation_id": "c"}),
        body={"model": "grok-4", "messages": [{"role": "user", "content": "q"}]},
        stream=False,
        resp_headers={},
    )
    assert isinstance(out, JSONResponse)
    body = out.body.decode()
    # Anthropic shape markers
    assert '"type": "message"' in body or '"type":"message"' in body
    assert '"role": "assistant"' in body or '"role":"assistant"' in body
    assert "answer" in body


@pytest.mark.asyncio
async def test_anthropic_dispatch_passes_system_block_to_messages(monkeypatch):
    """Anthropic ``system`` field is separate from messages; we prepend
    it as a system message before flattening for grok.com."""
    captured = {}

    async def fake_complete(extra_config, *, messages, model, **kw):
        captured["messages"] = messages
        return {"id": "x", "model": "grok-3",
                "choices": [{"index": 0, "message": {"content": ""}}]}

    monkeypatch.setattr("app.providers.grok_web.complete_grok_web", fake_complete)

    await dispatch_grok_web_anthropic(
        route=_route({"bridge_url": "http://b", "conversation_id": "c"}),
        body={
            "model": "grok-3",
            "system": "You are concise.",
            "messages": [{"role": "user", "content": "hi"}],
        },
        stream=False,
        resp_headers={},
    )
    # Should have prepended system as a message
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][0]["content"] == "You are concise."


@pytest.mark.asyncio
async def test_anthropic_dispatch_no_system_block_no_prepend(monkeypatch):
    """No system → no fake system message added."""
    captured = {}

    async def fake_complete(extra_config, *, messages, model, **kw):
        captured["messages"] = messages
        return {"id": "x", "model": "grok-3",
                "choices": [{"index": 0, "message": {"content": ""}}]}

    monkeypatch.setattr("app.providers.grok_web.complete_grok_web", fake_complete)

    await dispatch_grok_web_anthropic(
        route=_route({"bridge_url": "http://b", "conversation_id": "c"}),
        body={"model": "grok-3", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
        resp_headers={},
    )
    # Original list intact, no system prepended
    assert captured["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_anthropic_dispatch_streaming_returns_streaming_response(monkeypatch):
    async def fake_stream(extra_config, *, messages, system, model, **kw):
        yield b"event: message_start\ndata: {}\n\n"
        yield b"event: message_stop\ndata: {}\n\n"

    monkeypatch.setattr("app.providers.grok_web.stream_grok_web_anthropic", fake_stream)

    out = await dispatch_grok_web_anthropic(
        route=_route({"bridge_url": "http://b", "conversation_id": "c"}),
        body={"model": "grok-3", "messages": [{"role": "user", "content": "x"}]},
        stream=True,
        resp_headers={},
    )
    assert isinstance(out, StreamingResponse)
    assert out.media_type == "text/event-stream"
