"""v5.7.19 (refactor) — messages.py sub-blocks 2 + 3 extracted to
``_messages_pre_route``.

- Sub-block 2: input validation + model normalization + auto-resolution
  → ``normalize_request_body``.
- Sub-block 3: Anthropic→OpenAI body translation
  → ``translate_to_openai_if_needed``.

Combined into one ship because they target the same helper module
and the same handler (messages.py). Phase 1 of the refactor proposal
is now complete; Phase 2 (completions.py mirror with shared helpers)
follows.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


# ── structural pins ────────────────────────────────────────────────────


def test_helpers_exist():
    from app.api._messages_pre_route import (  # noqa: F401
        normalize_request_body,
        translate_to_openai_if_needed,
    )


def test_messages_handler_calls_normalize():
    """(v5.7.23: normalize_request_body moved to _handler_shared.)"""
    src = Path("app/api/messages.py").read_text()
    assert "from app.api._handler_shared import normalize_request_body" in src
    assert "await normalize_request_body(" in src


def test_messages_handler_calls_translate():
    src = Path("app/api/messages.py").read_text()
    assert "from app.api._messages_pre_route import translate_to_openai_if_needed" in src
    assert "translate_to_openai_if_needed(" in src


def test_normalize_signature():
    """The helper's positional args match the messages.py call site —
    catches drift."""
    import inspect
    from app.api._messages_pre_route import normalize_request_body
    sig = inspect.signature(normalize_request_body)
    params = list(sig.parameters.keys())
    assert params == ["body", "x_webhook_url", "db"], params


def test_translate_kwargs_only():
    """The translation helper uses keyword-only args after the *
    marker — catches accidental positional-arg refactors that would
    break the call site silently."""
    import inspect
    from app.api._messages_pre_route import translate_to_openai_if_needed
    sig = inspect.signature(translate_to_openai_if_needed)
    keyword_only = [
        n for n, p in sig.parameters.items()
        if p.kind == inspect.Parameter.KEYWORD_ONLY
    ]
    for required in [
        "body", "route", "system", "messages_list",
        "tools", "has_tool_blocks", "has_images",
    ]:
        assert required in keyword_only, (
            f"v5.7.19: '{required}' must be keyword-only on the translate helper"
        )


def test_inline_blocks_gone_from_messages_py():
    """Pin: the version-comment markers that delimited the extracted
    sub-blocks no longer appear inline in messages.py. Specifically
    the v3.10.0 Fix B widened-translation block AND the v3.5.8
    validation block."""
    src = Path("app/api/messages.py").read_text()
    # v3.10.0 block was the OpenAI translation. The inline rationale
    # comment moved into the helper's docstring.
    inline_marker_1 = "v3.10.0 (#269 Fix B, widened) — Anthropic→OpenAI body translation"
    assert inline_marker_1 not in src, (
        "v5.7.19: translate-to-openai inline block was re-inlined into messages.py."
    )
    inline_marker_2 = "v3.5.8 BUG-005 fix — validate request shape at the input boundary"
    assert inline_marker_2 not in src, (
        "v5.7.19: validate-request-shape inline block was re-inlined into messages.py."
    )


def test_messages_py_size_dropped_further():
    """Sub-blocks 2 + 3 should take messages.py below 1100 LOC. The
    file was 1138 after sub-block 1 (v5.7.18); after 2 + 3 it should
    be <= 1080 LOC. If new code was added between extracts, the
    threshold makes that visible."""
    src = Path("app/api/messages.py").read_text()
    n = src.count("\n") + 1
    assert n <= 1080, (
        f"v5.7.19: messages.py is {n} LOC; expected <= 1080 after all three "
        f"Phase 1 sub-block extracts."
    )


def test_version_bumped():
    """v5.7.19 minimum — later patches keep this passing."""
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (5, 7, 19), f"v5.7.19 must be reachable; got {__version__}"


# ── behavioural pins ───────────────────────────────────────────────────


def test_translate_passes_through_when_not_needed():
    """When route is claude-oauth, OR tool_emulation engaged, OR the
    body has no content blocks / tools / images, the helper passes
    inputs through unchanged with translated=False."""
    from app.api._messages_pre_route import translate_to_openai_if_needed
    route = MagicMock()
    route.profile.provider_type = "claude-oauth"
    route.tool_emulation_engaged = False
    route.cross_family_fallback = False
    body_in = {"model": "claude", "messages": []}
    body_out, system, msgs, tools, translated = translate_to_openai_if_needed(
        body=body_in, route=route, system="hi",
        messages_list=[], tools=None,
        has_tool_blocks=False, has_images=False,
    )
    assert translated is False
    assert body_out is body_in
    assert system == "hi"
    assert msgs == []
    assert tools is None


def test_translate_skipped_for_tool_emulation():
    """tool_emulation_engaged → skip translation even if cross_family
    is true (the emulation has its own Anthropic-shape prompt path)."""
    from app.api._messages_pre_route import translate_to_openai_if_needed
    route = MagicMock()
    route.profile.provider_type = "gemini"
    route.tool_emulation_engaged = True
    route.cross_family_fallback = True
    _, _, _, _, translated = translate_to_openai_if_needed(
        body={"model": "x", "messages": []}, route=route,
        system=None, messages_list=[{"role": "user", "content": [{"type": "image"}]}],
        tools=None, has_tool_blocks=False, has_images=True,
    )
    assert translated is False
