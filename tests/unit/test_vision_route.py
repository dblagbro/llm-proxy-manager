"""Unit tests for vision-to-text routing (Wave 5 #25)."""
import sys
import types
import asyncio
import pytest

# Stub heavy deps before app imports
sys.modules.setdefault("litellm", types.ModuleType("litellm"))

# Stub sqlalchemy for pick_vision_provider
_sqla = types.ModuleType("sqlalchemy")
_sqla_ext = types.ModuleType("sqlalchemy.ext")
_sqla_ext_async = types.ModuleType("sqlalchemy.ext.asyncio")
_sqla_ext_async.AsyncSession = object
sys.modules.setdefault("sqlalchemy", _sqla)
sys.modules.setdefault("sqlalchemy.ext", _sqla_ext)
sys.modules.setdefault("sqlalchemy.ext.asyncio", _sqla_ext_async)

from app.api.vision_route import (
    transcribe_anthropic,
    transcribe_openai,
    _VISION_PROMPT,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _msg_text(text="hello"):
    return {"role": "user", "content": text}


def _msg_image_anthropic(data="abc123", media_type="image/png"):
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
        ],
    }


def _msg_image_openai(url="https://example.com/img.png"):
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }


class _FakeVisionProvider:
    id = "vp1"
    name = "FakeVision"
    default_model = "gpt-4o"
    provider_type = "openai"
    api_key = "fake"
    base_url = None
    extra_headers = {}
    priority = 1


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestVisionPrompt:
    def test_prompt_includes_description_and_text_labels(self):
        assert "DESCRIPTION:" in _VISION_PROMPT
        assert "TEXT:" in _VISION_PROMPT


class TestTranscribeAnthropicNoProvider:
    """When no vision provider is available, fall back to strip_images_anthropic."""

    @pytest.mark.asyncio
    async def test_no_images_unchanged(self, monkeypatch):
        """No image blocks → messages pass through untouched, count=0."""
        import app.api.vision_route as vr

        async def _no_provider(db, excl=None):
            return None

        monkeypatch.setattr(vr, "pick_vision_provider", _no_provider)

        msgs = [_msg_text("hello")]
        out, count = await transcribe_anthropic(msgs, db=None)
        assert count == 0
        assert out[0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_fallback_strips_images_when_no_provider(self, monkeypatch):
        """No vision provider → calls strip_images_anthropic, returns 0 count."""
        import app.api.vision_route as vr

        async def _no_provider(db, excl=None):
            return None

        monkeypatch.setattr(vr, "pick_vision_provider", _no_provider)

        # strip_images_anthropic must be importable and return a list
        from app.api.image_utils import strip_images_anthropic
        msgs = [_msg_image_anthropic()]
        out, count = await transcribe_anthropic(msgs, db=None)
        assert count == 0
        # The fallback removes image blocks
        for msg in out:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    assert block.get("type") != "image"


class TestTranscribeOpenAINoProvider:
    @pytest.mark.asyncio
    async def test_no_images_unchanged(self, monkeypatch):
        import app.api.vision_route as vr

        async def _no_provider(db, excl=None):
            return None

        monkeypatch.setattr(vr, "pick_vision_provider", _no_provider)

        msgs = [_msg_text("hello")]
        out, count = await transcribe_openai(msgs, db=None)
        assert count == 0

    @pytest.mark.asyncio
    async def test_fallback_strips_images_when_no_provider(self, monkeypatch):
        import app.api.vision_route as vr

        async def _no_provider(db, excl=None):
            return None

        monkeypatch.setattr(vr, "pick_vision_provider", _no_provider)

        msgs = [_msg_image_openai()]
        out, count = await transcribe_openai(msgs, db=None)
        assert count == 0
        for msg in out:
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    assert part.get("type") != "image_url"


class TestTranscribeAnthropicWithProvider:
    @pytest.mark.asyncio
    async def test_image_replaced_with_text_block(self, monkeypatch):
        import app.api.vision_route as vr

        async def _has_provider(db, excl=None):
            return _FakeVisionProvider()

        async def _fake_describe(block, provider):
            return "DESCRIPTION: a cat\nTEXT: none"

        monkeypatch.setattr(vr, "pick_vision_provider", _has_provider)
        monkeypatch.setattr(vr, "_describe_one_image_anthropic_block", _fake_describe)

        msgs = [_msg_image_anthropic()]
        out, count = await transcribe_anthropic(msgs, db=None)
        assert count == 1
        content = out[0]["content"]
        assert isinstance(content, list)
        text_blocks = [b for b in content if b.get("type") == "text"]
        assert any("FakeVision" in b["text"] for b in text_blocks)
        # No image blocks remain
        assert all(b.get("type") != "image" for b in content)

    @pytest.mark.asyncio
    async def test_multiple_images_all_transcribed(self, monkeypatch):
        import app.api.vision_route as vr

        call_count = {"n": 0}

        async def _has_provider(db, excl=None):
            return _FakeVisionProvider()

        async def _fake_describe(block, provider):
            call_count["n"] += 1
            return f"DESCRIPTION: image {call_count['n']}\nTEXT: none"

        monkeypatch.setattr(vr, "pick_vision_provider", _has_provider)
        monkeypatch.setattr(vr, "_describe_one_image_anthropic_block", _fake_describe)

        two_image_msg = {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "a"}},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "b"}},
            ],
        }
        out, count = await transcribe_anthropic([two_image_msg], db=None)
        assert count == 2
        text_blocks = [b for b in out[0]["content"] if b.get("type") == "text"]
        assert len(text_blocks) == 2


class TestTranscribeOpenAIWithProvider:
    @pytest.mark.asyncio
    async def test_image_url_replaced_with_text(self, monkeypatch):
        import app.api.vision_route as vr

        async def _has_provider(db, excl=None):
            return _FakeVisionProvider()

        async def _fake_describe(part, provider):
            return "DESCRIPTION: a dog\nTEXT: none"

        monkeypatch.setattr(vr, "pick_vision_provider", _has_provider)
        monkeypatch.setattr(vr, "_describe_one_image_openai_part", _fake_describe)

        msgs = [_msg_image_openai()]
        out, count = await transcribe_openai(msgs, db=None)
        assert count == 1
        content = out[0]["content"]
        text_parts = [p for p in content if p.get("type") == "text"]
        assert any("FakeVision" in p["text"] for p in text_parts)
        assert all(p.get("type") != "image_url" for p in content)
