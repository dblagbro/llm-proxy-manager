"""v5.3.6 — cursor-oauth list-content → string emulation tests.

JiuZ-Chn cursor-to-openai bridge rejects messages with list content
("request.messages.content: string expected"). Real OpenAI accepts
both. This batch coerces in the dispatch path so every caller benefits
without each having to know about the bridge's stricter shape.

Provider-adapter emulation scope, NOT request mutation — we're matching
a specific upstream's shape constraint, not silently rewriting caller
intent across providers.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Source pins ─────────────────────────────────────────────────────


def test_normalize_helper_exported():
    from app.providers.cursor_oauth import normalize_messages_for_bridge
    assert callable(normalize_messages_for_bridge)


def test_retry_wrapper_calls_normalize_when_routing_to_bridge():
    """``acompletion_with_retry`` must invoke the normalize helper when
    the api_base points at the cursor-bridge sidecar. Source pin so a
    future refactor can't drop the hook silently."""
    src = Path("app/routing/retry.py").read_text()
    assert "normalize_messages_for_bridge" in src
    assert "cursor-bridge" in src


# ── Behavioral — the normalize helper ───────────────────────────────


def test_string_content_unchanged():
    from app.providers.cursor_oauth import normalize_messages_for_bridge
    msgs = [{"role": "user", "content": "hello"}]
    out = normalize_messages_for_bridge(msgs)
    assert out == msgs


def test_null_content_preserved():
    """Assistant tool_calls turns legitimately carry content=null —
    the bridge accepts this shape natively."""
    from app.providers.cursor_oauth import normalize_messages_for_bridge
    msgs = [{"role": "assistant", "content": None, "tool_calls": [{"id": "a"}]}]
    out = normalize_messages_for_bridge(msgs)
    assert out == msgs


def test_list_of_text_parts_joined_to_string():
    from app.providers.cursor_oauth import normalize_messages_for_bridge
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ],
    }]
    out = normalize_messages_for_bridge(msgs)
    assert isinstance(out[0]["content"], str)
    assert out[0]["content"] == "first\nsecond"


def test_responses_shape_input_text_recognized():
    """OpenAI Responses API uses input_text/output_text instead of text."""
    from app.providers.cursor_oauth import normalize_messages_for_bridge
    msgs = [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": "from responses"},
            {"type": "output_text", "text": "another"},
        ],
    }]
    out = normalize_messages_for_bridge(msgs)
    assert out[0]["content"] == "from responses\nanother"


def test_image_part_placeholder():
    from app.providers.cursor_oauth import normalize_messages_for_bridge
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,XXX"}},
        ],
    }]
    out = normalize_messages_for_bridge(msgs)
    assert out[0]["content"] == "look at this\n[image]"


def test_refusal_part_surfaced():
    from app.providers.cursor_oauth import normalize_messages_for_bridge
    msgs = [{
        "role": "assistant",
        "content": [{"type": "refusal", "refusal": "cannot help"}],
    }]
    out = normalize_messages_for_bridge(msgs)
    assert "[refusal: cannot help]" in out[0]["content"]


def test_unknown_block_dumped_not_dropped():
    """Future block types must surface, not silently vanish, so the
    bridge logs show the unexpected shape."""
    from app.providers.cursor_oauth import normalize_messages_for_bridge
    msgs = [{
        "role": "user",
        "content": [{"type": "audio", "audio_id": "xyz", "format": "wav"}],
    }]
    out = normalize_messages_for_bridge(msgs)
    # JSON dump of the unknown block — content must contain the type name
    assert "audio" in out[0]["content"]
    assert "audio_id" in out[0]["content"]


def test_multiple_messages_mixed_shapes():
    """One pass through a real conversation: system string, user list,
    assistant string, user list with mixed parts. All shapes resolve."""
    from app.providers.cursor_oauth import normalize_messages_for_bridge
    msgs = [
        {"role": "system", "content": "you are a helpful assistant"},
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]},
    ]
    out = normalize_messages_for_bridge(msgs)
    assert out[0]["content"] == "you are a helpful assistant"  # untouched
    assert out[1]["content"] == "hi"
    assert out[2]["content"] == "hello"  # untouched
    assert out[3]["content"] == "describe\n[image]"


def test_non_list_input_returns_as_is():
    """Defensive — if some caller passes None / dict / etc., don't crash."""
    from app.providers.cursor_oauth import normalize_messages_for_bridge
    assert normalize_messages_for_bridge(None) is None  # type: ignore[arg-type]
    assert normalize_messages_for_bridge({}) == {}  # type: ignore[arg-type]


def test_non_dict_message_passed_through():
    """Defensive — messages list with a non-dict entry doesn't crash."""
    from app.providers.cursor_oauth import normalize_messages_for_bridge
    msgs = ["weird", {"role": "user", "content": [{"type": "text", "text": "ok"}]}]
    out = normalize_messages_for_bridge(msgs)
    assert out[0] == "weird"
    assert out[1]["content"] == "ok"


# ── Behavioral — the retry wrapper hook ─────────────────────────────


@pytest.mark.asyncio
async def test_retry_wrapper_normalizes_when_api_base_is_cursor_bridge():
    """End-to-end: when ``api_base`` contains ``cursor-bridge``, the
    messages list passed to ``litellm.acompletion`` has been normalized."""
    from unittest.mock import AsyncMock, patch
    from app.routing.retry import acompletion_with_retry

    captured_messages: list = []

    async def fake_acompletion(model, messages, **kwargs):
        captured_messages.extend(messages)
        return {"choices": [{"message": {"content": "ok"}}]}

    with patch("app.routing.retry.litellm.acompletion", new=AsyncMock(side_effect=fake_acompletion)):
        await acompletion_with_retry(
            model="openai/coordinator-code",
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            api_base="http://llm-proxy2-cursor-bridge:3010/v1",
        )

    assert captured_messages[0]["content"] == "hi"
    assert isinstance(captured_messages[0]["content"], str)


@pytest.mark.asyncio
async def test_retry_wrapper_does_not_normalize_for_non_bridge_api_base():
    """When ``api_base`` doesn't point at the bridge, messages must be
    left alone — real OpenAI / Anthropic / Gemini accept list-content."""
    from unittest.mock import AsyncMock, patch
    from app.routing.retry import acompletion_with_retry

    captured_messages: list = []

    async def fake_acompletion(model, messages, **kwargs):
        captured_messages.extend(messages)
        return {"choices": [{"message": {"content": "ok"}}]}

    orig_content = [{"type": "text", "text": "hi"}]
    with patch("app.routing.retry.litellm.acompletion", new=AsyncMock(side_effect=fake_acompletion)):
        await acompletion_with_retry(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": orig_content}],
            api_base="https://api.openai.com/v1",
        )

    # Content stayed a list — no coercion for non-cursor-bridge dispatch
    assert captured_messages[0]["content"] == orig_content
