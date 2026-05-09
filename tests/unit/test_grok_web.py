"""Unit tests for the grok-web provider's helpers + dispatchers.

Covers v3.2.0 manual mode (HTTP replay against grok.com using cookies
stored on Provider.extra_config) and v3.2.1 bridge mode (forward to a
Playwright sidecar's /api/chat). Network calls are mocked via httpx's
MockTransport so the tests never touch real grok.com or a real bridge.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.providers.grok_web import (
    DEFAULT_MODEL,
    GrokWebAuthError,
    GrokWebError,
    MODEL_TO_MODE_ID,
    SUPPORTED_MODELS,
    _bridge_chat,
    _build_body,
    _build_headers,
    _flatten_messages_to_prompt,
    _is_bridge_mode,
    _model_to_mode_id,
    _validate_extra_config,
    anthropic_response_from_openai,
    complete_grok_web,
    stream_grok_web,
    stream_grok_web_anthropic,
)


# ── Static config / catalog ─────────────────────────────────────────────


def test_supported_models_includes_xai_aliases():
    """v3.2.8 regression: callers send `x-ai/grok-4` (OpenRouter slug
    style). The router scores by exact capability match, so grok-web
    must claim both bare and prefixed variants or it loses to OpenRouter
    despite being priority=1. Pre-v3.2.8 only ['grok-3','grok-4'] were
    listed → 6 of 8 grok requests routed to OpenRouter in 24h."""
    assert "grok-3" in SUPPORTED_MODELS
    assert "grok-4" in SUPPORTED_MODELS
    assert "x-ai/grok-3" in SUPPORTED_MODELS
    assert "x-ai/grok-4" in SUPPORTED_MODELS


def test_default_model_is_grok_3():
    """grok-3 (modeId=fast) is the Lite-plan default; grok-4 (expert)
    is opt-in per request. Don't accidentally default to grok-4 — its
    higher per-message reasoning cost would surprise operators."""
    assert DEFAULT_MODEL == "grok-3"


def test_model_to_mode_id_handles_both_slug_styles():
    assert _model_to_mode_id("grok-3") == "fast"
    assert _model_to_mode_id("grok-4") == "expert"
    assert _model_to_mode_id("x-ai/grok-3") == "fast"
    assert _model_to_mode_id("x-ai/grok-4") == "expert"
    # Unknown / fall-through → fast (grok-3) — operator's Lite plan
    # always grants this; expert may not.
    assert _model_to_mode_id("") == "fast"
    assert _model_to_mode_id("unknown-model") == "fast"


def test_model_to_mode_id_table_complete():
    """Every entry in SUPPORTED_MODELS must map to a valid modeId,
    otherwise a request to that capability would silently default
    to 'fast'."""
    for m in SUPPORTED_MODELS:
        assert _model_to_mode_id(m) in ("fast", "expert"), m
        assert m in MODEL_TO_MODE_ID, f"{m} missing from MODEL_TO_MODE_ID"


# ── Mode detection + validation ─────────────────────────────────────────


def test_is_bridge_mode():
    """Bridge mode is signaled solely by ``extra_config.bridge_url``.
    Empty/missing → manual mode."""
    assert _is_bridge_mode({"bridge_url": "http://x"}) is True
    assert _is_bridge_mode({"bridge_url": "https://www.voipguru.org/grok-bridge"}) is True
    assert _is_bridge_mode({}) is False
    assert _is_bridge_mode({"bridge_url": ""}) is False
    assert _is_bridge_mode(None) is False


def test_validate_manual_requires_cookie_and_conv():
    """Manual mode: cookie_header + conversation_id both required.
    Missing either → GrokWebError(status_code=400)."""
    with pytest.raises(GrokWebError) as ex:
        _validate_extra_config({})
    assert ex.value.status_code == 400
    assert "cookie_header" in str(ex.value)
    assert "conversation_id" in str(ex.value)

    with pytest.raises(GrokWebError):
        _validate_extra_config({"cookie_header": "x"})  # missing conv_id

    with pytest.raises(GrokWebError):
        _validate_extra_config({"conversation_id": "uuid"})  # missing cookies

    # Both present → valid (no raise)
    _validate_extra_config({
        "cookie_header": "cf_clearance=x; sso=y",
        "conversation_id": "e41fca28-3df3-44ae-ad27-1cb65d5fe2a5",
    })


def test_validate_bridge_only_needs_conv():
    """Bridge mode: conversation_id required; cookies live on the
    bridge side. Pre-v3.2.3 the validator hard-required cookie_header
    too → form blocked even with valid bridge config."""
    with pytest.raises(GrokWebError) as ex:
        _validate_extra_config({"bridge_url": "http://b"})
    assert ex.value.status_code == 400
    assert "conversation_id" in str(ex.value)

    # bridge_url + conversation_id → valid even WITHOUT cookie_header
    _validate_extra_config({
        "bridge_url": "http://llm-proxy2-grok-bridge:8443",
        "conversation_id": "e41fca28-3df3-44ae-ad27-1cb65d5fe2a5",
    })


# ── Header / body construction ──────────────────────────────────────────


def test_build_headers_emits_required_fields():
    cfg = {
        "cookie_header": "cf_clearance=abc; __cf_bm=def; sso=jwt",
        "user_agent": "Mozilla/5.0 (Test) Chrome/147",
        "x_statsig_id": "stats-id-xyz",
        "x_userid": "user-uuid",
    }
    h = _build_headers(cfg, "conv-uuid")
    assert h["cookie"] == cfg["cookie_header"]
    assert h["user-agent"] == cfg["user_agent"]
    assert h["x-statsig-id"] == cfg["x_statsig_id"]
    assert h["x-userid"] == cfg["x_userid"]
    assert h["referer"] == "https://grok.com/c/conv-uuid"
    assert h["origin"] == "https://grok.com"
    # Each request gets a unique x-xai-request-id
    assert "x-xai-request-id" in h
    h2 = _build_headers(cfg, "conv-uuid")
    assert h["x-xai-request-id"] != h2["x-xai-request-id"]


def test_build_headers_uses_default_ua_when_missing():
    """Operator may not paste a UA; we ship a sane Chrome 147 default."""
    h = _build_headers({"cookie_header": ""}, "c1")
    assert "Chrome/147.0.0.0" in h["user-agent"]


def test_build_body_shape_matches_browser():
    """Captured browser request shape from 2026-05-08 reverse-engineering.
    Several flags are non-obvious; if any drift, grok.com may reject."""
    b = _build_body("hello", "fast", parent_response_id="parent-uuid")
    assert b["message"] == "hello"
    assert b["modeId"] == "fast"
    assert b["parentResponseId"] == "parent-uuid"
    # Defaults that grok.com expects to see set
    assert b["disableSearch"] is False
    assert b["forceConcise"] is True
    assert b["sendFinalMetadata"] is True
    # deviceEnvInfo is required — missing it returns 400
    assert "deviceEnvInfo" in b
    assert b["deviceEnvInfo"]["devicePixelRatio"] == 1.0


def test_build_body_default_parent_response_id_empty():
    """Each proxy call defaults to parentResponseId='' so callers don't
    bleed context inside the operator's shared conversation."""
    b = _build_body("hi", "fast")
    assert b["parentResponseId"] == ""


# ── Message flattening ──────────────────────────────────────────────────


def test_flatten_single_user_unwrapped():
    """A bare single user turn shouldn't get [User] scaffolding —
    grok.com gets the cleanest possible prompt."""
    out = _flatten_messages_to_prompt([{"role": "user", "content": "hi"}])
    assert out == "hi"


def test_flatten_multi_turn_labels_each_role():
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"},
        {"role": "user", "content": "again"},
    ]
    out = _flatten_messages_to_prompt(msgs)
    assert "[System]" in out
    assert "[User]" in out
    assert "[Assistant]" in out
    assert "ping" in out and "pong" in out and "again" in out


def test_flatten_anthropic_content_blocks():
    """Anthropic-style content can be a list of typed blocks. Text
    blocks should be concatenated; tool_use/tool_result get serialized."""
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Compute 2+2"},
            {"type": "tool_use", "name": "calc", "input": {"expr": "2+2"}},
        ],
    }]
    out = _flatten_messages_to_prompt(msgs)
    assert "Compute 2+2" in out
    assert "tool_use" in out  # serialized fallback
    assert "calc" in out


# ── Response shape conversion ───────────────────────────────────────────


def test_anthropic_response_from_openai_basic():
    """Lossy translation: OpenAI shape → Anthropic /v1/messages shape."""
    openai = {
        "id": "chatcmpl-x",
        "model": "grok-4",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
    anth = anthropic_response_from_openai(openai)
    assert anth["type"] == "message"
    assert anth["role"] == "assistant"
    assert anth["model"] == "grok-4"
    assert anth["content"][0]["text"] == "hi"
    assert anth["content"][0]["type"] == "text"
    assert anth["usage"]["input_tokens"] == 5
    assert anth["usage"]["output_tokens"] == 2
    assert anth["stop_reason"] == "end_turn"


def test_anthropic_response_handles_empty_text():
    """Defensive: missing/empty content should still produce a valid
    Anthropic response (empty text block, not a crash)."""
    anth = anthropic_response_from_openai({})
    assert anth["content"][0]["text"] == ""
    assert anth["model"] == "grok-3"


# ── Bridge dispatch (mocked transport) ──────────────────────────────────


def _bridge_handler_factory(*, status: int = 200, body: dict | None = None,
                            captured: list | None = None):
    """Build a httpx MockTransport handler that captures incoming
    requests and returns a canned response."""
    body = body or {
        "id": "bridge-result",
        "model": "grok-3",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        if status == 200:
            return httpx.Response(200, json=body)
        return httpx.Response(status, text="bridge upstream error body")

    return handler


@pytest.mark.asyncio
async def test_bridge_chat_sends_token_and_payload(monkeypatch):
    """Bridge mode forwards messages + model + conversation_id to
    /api/chat with X-Bridge-Token. Failure to send the token would
    let any peer hit the bridge anonymously."""
    captured: list = []
    handler = _bridge_handler_factory(captured=captured)

    # Patch httpx.AsyncClient to use our MockTransport
    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.providers.grok_web.httpx.AsyncClient", patched_client)

    cfg = {
        "bridge_url": "https://www.voipguru.org/grok-bridge",
        "bridge_token": "secret-token-123",
        "conversation_id": "conv-uuid",
    }
    out = await _bridge_chat(cfg, [{"role": "user", "content": "hi"}], "grok-3",
                             stream=False, timeout=10.0)
    assert out["id"] == "bridge-result"

    # Verify request shape
    assert len(captured) == 1
    req = captured[0]
    assert req.url.path.endswith("/api/chat")
    assert req.headers.get("x-bridge-token") == "secret-token-123"
    payload = json.loads(req.content)
    assert payload["model"] == "grok-3"
    assert payload["conversation_id"] == "conv-uuid"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_bridge_chat_401_raises_auth_error(monkeypatch):
    """Bridge returning 401 → operator must re-sign-in. Surfaced as
    GrokWebAuthError so /v1/chat/completions returns 401 (not 502)."""
    handler = _bridge_handler_factory(status=401)
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.providers.grok_web.httpx.AsyncClient", patched)

    cfg = {
        "bridge_url": "http://x",
        "bridge_token": "t",
        "conversation_id": "c",
    }
    with pytest.raises(GrokWebAuthError):
        await _bridge_chat(cfg, [{"role": "user", "content": "x"}], "grok-3",
                           stream=False, timeout=5.0)


@pytest.mark.asyncio
async def test_bridge_chat_500_raises_generic_error(monkeypatch):
    handler = _bridge_handler_factory(status=500)
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.providers.grok_web.httpx.AsyncClient", patched)

    cfg = {"bridge_url": "http://x", "conversation_id": "c"}
    with pytest.raises(GrokWebError) as ex:
        await _bridge_chat(cfg, [{"role": "user", "content": "x"}], "grok-3",
                           stream=False, timeout=5.0)
    assert ex.value.status_code == 502


@pytest.mark.asyncio
async def test_bridge_chat_network_error(monkeypatch):
    """ConnectError → GrokWebError with informative message. Operator
    gets a clear 'bridge unreachable' instead of a Python traceback."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.providers.grok_web.httpx.AsyncClient", patched)

    cfg = {"bridge_url": "http://x", "conversation_id": "c"}
    with pytest.raises(GrokWebError) as ex:
        await _bridge_chat(cfg, [{"role": "user", "content": "x"}], "grok-3",
                           stream=False, timeout=5.0)
    assert "unreachable" in str(ex.value).lower()


@pytest.mark.asyncio
async def test_complete_grok_web_routes_to_bridge_when_url_set(monkeypatch):
    """End-to-end: complete_grok_web with bridge_url present → calls
    bridge /api/chat (not grok.com directly). Confirms the v3.2.1
    branch fires."""
    handler = _bridge_handler_factory()
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.providers.grok_web.httpx.AsyncClient", patched)

    cfg = {
        "bridge_url": "http://b",
        "bridge_token": "t",
        "conversation_id": "c",
    }
    out = await complete_grok_web(cfg, [{"role": "user", "content": "ping"}], "grok-3")
    assert out["choices"][0]["message"]["content"] == "OK"
