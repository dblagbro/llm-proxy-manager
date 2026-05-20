"""Unit tests for ``_pick_chat_model()`` in
``tests/integration/test_compatibility_matrix.py``.

Closes BUG-043/044/045: the matrix test pre-fix sent
``provider.default_model`` as the chat model name, which fails with HTTP
400 against any provider whose ``default_model`` is empty / null /
embedding-only. The picker resolves a chat-capable model via:
  1. ``default_model`` if non-empty + non-embedding
  2. scanned capability rows (preferring command-/gpt-/claude-/gemini-)
  3. per-provider-type hard-coded fallback table

These unit tests pin each branch + the caching behaviour.
"""
from __future__ import annotations

import pytest

# Import the picker directly. It lives in an integration-test file but is
# a pure helper — safe to unit-test in isolation.
from tests.integration.test_compatibility_matrix import (
    _pick_chat_model,
    _chat_model_cache,
    _PROVIDER_TYPE_CHAT_DEFAULT,
    _looks_embedding,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with a fresh cache so they don't interfere."""
    _chat_model_cache.clear()
    yield
    _chat_model_cache.clear()


class _FakeResp:
    def __init__(self, status_code: int, json_body=None):
        self.status_code = status_code
        self._json = json_body or []

    def json(self):
        return self._json


class _FakeSession:
    """Minimal admin_session stub — only the .get() the picker uses."""

    def __init__(self, capability_response: _FakeResp):
        self.calls: list[str] = []
        self._resp = capability_response

    def get(self, url, timeout=None):
        self.calls.append(url)
        return self._resp


# ── _looks_embedding ──────────────────────────────────────────────


@pytest.mark.parametrize("slug", [
    "embed-english-v3.0", "text-embedding-3-large",
    "cohere-embed-v2", "rerank-english-v2", "vector-store-small",
])
def test_looks_embedding_positive(slug):
    assert _looks_embedding(slug) is True


@pytest.mark.parametrize("slug", [
    "gpt-4o", "claude-sonnet-4-6", "gemini-2.5-flash",
    "command-r", "grok-3", "openrouter/openai/gpt-4o",
])
def test_looks_embedding_negative(slug):
    assert _looks_embedding(slug) is False


# ── Branch 1: default_model used when usable ──────────────────────


def test_uses_default_model_when_non_empty_non_embedding():
    s = _FakeSession(_FakeResp(200, []))
    provider = {"id": "p1", "provider_type": "anthropic",
                "default_model": "claude-sonnet-4-6"}
    assert _pick_chat_model(s, provider) == "claude-sonnet-4-6"
    # No capability lookup needed when default_model is good
    assert s.calls == []


# ── Branch 2: capabilities ────────────────────────────────────────


def test_falls_through_when_default_model_empty():
    s = _FakeSession(_FakeResp(200, [
        {"model_id": "gemini-2.5-flash", "tasks": ["chat"]},
        {"model_id": "text-embedding-3-large", "tasks": ["embed"]},
    ]))
    provider = {"id": "p2", "provider_type": "openrouter",
                "default_model": ""}
    assert _pick_chat_model(s, provider) == "gemini-2.5-flash"
    assert len(s.calls) == 1


def test_falls_through_when_default_model_is_embedding():
    s = _FakeSession(_FakeResp(200, [
        {"model_id": "command-r", "tasks": ["chat"]},
        {"model_id": "c4ai-aya-expanse-32b", "tasks": ["chat"]},
    ]))
    provider = {"id": "p3", "provider_type": "cohere",
                "default_model": "embed-english-v3.0"}
    # Preference order picks command-r over alphabetical first
    assert _pick_chat_model(s, provider) == "command-r"


def test_falls_through_when_default_model_is_null():
    s = _FakeSession(_FakeResp(200, [
        {"model_id": "claude-haiku-4-5", "tasks": ["chat"]},
    ]))
    provider = {"id": "p4", "provider_type": "anthropic",
                "default_model": None}
    assert _pick_chat_model(s, provider) == "claude-haiku-4-5"


def test_capability_preference_order():
    """Among multiple chat-capable rows, the picker prefers command-/gpt-/
    claude-/gemini-/grok- prefixes in that order."""
    rows = [
        {"model_id": "zzz-other-chat", "tasks": ["chat"]},
        {"model_id": "claude-sonnet-4-6", "tasks": ["chat"]},
        {"model_id": "gpt-4o-mini", "tasks": ["chat"]},
        {"model_id": "command-r", "tasks": ["chat"]},
    ]
    s = _FakeSession(_FakeResp(200, rows))
    provider = {"id": "p5", "provider_type": "openrouter",
                "default_model": ""}
    # command- wins (highest preference)
    assert _pick_chat_model(s, provider) == "command-r"


def test_excludes_embedding_capability_rows():
    s = _FakeSession(_FakeResp(200, [
        {"model_id": "text-embedding-3-large", "tasks": ["chat"]},  # name-embed
        {"model_id": "gpt-4o-mini",            "tasks": ["chat"]},
    ]))
    provider = {"id": "p6", "provider_type": "openai",
                "default_model": ""}
    assert _pick_chat_model(s, provider) == "gpt-4o-mini"


def test_excludes_rows_without_chat_in_tasks():
    s = _FakeSession(_FakeResp(200, [
        {"model_id": "voice-only-model", "tasks": ["voice"]},
        {"model_id": "gpt-4o-mini",      "tasks": ["chat"]},
    ]))
    provider = {"id": "p7", "provider_type": "openai",
                "default_model": ""}
    assert _pick_chat_model(s, provider) == "gpt-4o-mini"


# ── Branch 3: per-type fallback ───────────────────────────────────


def test_per_type_fallback_when_no_capabilities():
    """C1 Anthropic config case (BUG-045): 0 capability rows AND null
    default_model. The picker falls back to the type's default."""
    s = _FakeSession(_FakeResp(200, []))  # zero capability rows
    provider = {"id": "p8", "provider_type": "anthropic",
                "default_model": None}
    result = _pick_chat_model(s, provider)
    assert result == _PROVIDER_TYPE_CHAT_DEFAULT["anthropic"]
    assert result == "claude-haiku-4-5"


def test_per_type_fallback_for_every_known_type():
    """Every type in _PROVIDER_TYPE_CHAT_DEFAULT resolves cleanly when
    default_model is empty AND no capabilities are scanned."""
    for ptype, expected in _PROVIDER_TYPE_CHAT_DEFAULT.items():
        _chat_model_cache.clear()
        s = _FakeSession(_FakeResp(200, []))
        provider = {"id": f"p-{ptype}", "provider_type": ptype,
                    "default_model": ""}
        assert _pick_chat_model(s, provider) == expected, \
            f"per-type fallback failed for {ptype!r}"


def test_capability_endpoint_500_falls_through_to_per_type():
    s = _FakeSession(_FakeResp(500, None))
    provider = {"id": "p9", "provider_type": "cohere",
                "default_model": "embed-english-v3.0"}
    assert _pick_chat_model(s, provider) == _PROVIDER_TYPE_CHAT_DEFAULT["cohere"]


# ── Branch 4: ultimate fallback ───────────────────────────────────


def test_ultimate_fallback_for_unknown_provider_type():
    """A provider_type not in the table still gets gpt-4o-mini so the
    test doesn't crash. Documented as a config-drift signal."""
    s = _FakeSession(_FakeResp(200, []))
    provider = {"id": "p10", "provider_type": "completely-new-type",
                "default_model": ""}
    assert _pick_chat_model(s, provider) == "gpt-4o-mini"


# ── caching ───────────────────────────────────────────────────────


def test_result_is_cached_per_provider_id():
    s = _FakeSession(_FakeResp(200, [
        {"model_id": "gpt-4o-mini", "tasks": ["chat"]},
    ]))
    provider = {"id": "p11", "provider_type": "openai", "default_model": ""}
    a = _pick_chat_model(s, provider)
    b = _pick_chat_model(s, provider)
    assert a == b
    # Capability endpoint hit only once
    assert len(s.calls) == 1


def test_no_id_returns_none():
    """Defensive — a provider dict without an id can't be cached."""
    s = _FakeSession(_FakeResp(200, []))
    assert _pick_chat_model(s, {"provider_type": "openai"}) is None
