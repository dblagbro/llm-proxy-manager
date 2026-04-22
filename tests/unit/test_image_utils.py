"""Unit tests for image detection and stripping utilities (both wire formats)."""
import pytest
from app.api.image_utils import (
    has_images_anthropic,
    strip_images_anthropic,
    has_images_openai,
    strip_images_openai,
)

# ── has_images_anthropic ──────────────────────────────────────────────────────

def test_anthropic_no_images_string_content():
    msgs = [{"role": "user", "content": "hello"}]
    assert has_images_anthropic(msgs) is False


def test_anthropic_no_images_text_block():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert has_images_anthropic(msgs) is False


def test_anthropic_detects_image_block():
    msgs = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
    ]}]
    assert has_images_anthropic(msgs) is True


def test_anthropic_detects_image_url_block():
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x.com/img.png"}}]}]
    assert has_images_anthropic(msgs) is True


def test_anthropic_mixed_messages_returns_true_when_any_has_image():
    msgs = [
        {"role": "user", "content": "no image here"},
        {"role": "user", "content": [{"type": "image", "source": {}}]},
    ]
    assert has_images_anthropic(msgs) is True


def test_anthropic_empty_messages():
    assert has_images_anthropic([]) is False


# ── strip_images_anthropic ────────────────────────────────────────────────────

def test_strip_anthropic_replaces_image_with_text():
    msgs = [{"role": "user", "content": [
        {"type": "image", "source": {"media_type": "image/jpeg"}},
    ]}]
    result = strip_images_anthropic(msgs)
    assert len(result) == 1
    blocks = result[0]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "image/jpeg" in blocks[0]["text"]
    assert "not supported" in blocks[0]["text"]


def test_strip_anthropic_preserves_text_blocks():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "describe this"},
        {"type": "image", "source": {"media_type": "image/png"}},
    ]}]
    result = strip_images_anthropic(msgs)
    blocks = result[0]["content"]
    assert len(blocks) == 2
    assert blocks[0] == {"type": "text", "text": "describe this"}
    assert blocks[1]["type"] == "text"


def test_strip_anthropic_string_content_passes_through():
    msgs = [{"role": "user", "content": "plain text"}]
    result = strip_images_anthropic(msgs)
    assert result == msgs


def test_strip_anthropic_empty_messages():
    assert strip_images_anthropic([]) == []


def test_strip_anthropic_no_images_unchanged():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    result = strip_images_anthropic(msgs)
    assert result[0]["content"] == [{"type": "text", "text": "hello"}]


def test_strip_anthropic_uses_media_type_in_placeholder():
    msgs = [{"role": "user", "content": [
        {"type": "image", "source": {"media_type": "image/gif"}},
    ]}]
    result = strip_images_anthropic(msgs)
    assert "image/gif" in result[0]["content"][0]["text"]


# ── has_images_openai ─────────────────────────────────────────────────────────

def test_openai_no_images_string_content():
    msgs = [{"role": "user", "content": "hello"}]
    assert has_images_openai(msgs) is False


def test_openai_no_images_text_part():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert has_images_openai(msgs) is False


def test_openai_detects_image_url_part():
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]}]
    assert has_images_openai(msgs) is True


def test_openai_ignores_anthropic_image_type():
    msgs = [{"role": "user", "content": [{"type": "image", "source": {}}]}]
    assert has_images_openai(msgs) is False


def test_openai_mixed_messages():
    msgs = [
        {"role": "user", "content": "no image"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {}}]},
    ]
    assert has_images_openai(msgs) is True


# ── strip_images_openai ───────────────────────────────────────────────────────

def test_strip_openai_replaces_image_url_with_text():
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "http://x.com/img.png"}},
    ]}]
    result = strip_images_openai(msgs)
    parts = result[0]["content"]
    assert len(parts) == 1
    assert parts[0]["type"] == "text"
    assert "not supported" in parts[0]["text"]


def test_strip_openai_preserves_text_parts():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "what's in this image?"},
        {"type": "image_url", "image_url": {"url": "http://x.com/img.png"}},
    ]}]
    result = strip_images_openai(msgs)
    parts = result[0]["content"]
    assert len(parts) == 2
    assert parts[0] == {"type": "text", "text": "what's in this image?"}
    assert parts[1]["type"] == "text"


def test_strip_openai_string_content_passes_through():
    msgs = [{"role": "user", "content": "plain text"}]
    result = strip_images_openai(msgs)
    assert result == msgs


def test_strip_openai_empty_messages():
    assert strip_images_openai([]) == []


def test_strip_openai_no_images_unchanged():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    result = strip_images_openai(msgs)
    assert result[0]["content"] == [{"type": "text", "text": "hello"}]
