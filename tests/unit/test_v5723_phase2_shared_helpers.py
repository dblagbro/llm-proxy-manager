"""v5.7.23 (refactor Phase 2) — completions.py + messages.py
share the same pre-route helpers via a new ``_handler_shared`` module.

Phase 1 (v5.7.18/v5.7.19) extracted three sub-blocks from
messages.py into ``_messages_pre_route``. Two of those — request
context setup and body normalization — were repeated almost verbatim
in completions.py. Phase 2 lifts both to ``_handler_shared``,
parameterized by ``endpoint`` so each handler reuses the same code.

What stays in ``_messages_pre_route``:
- ``translate_to_openai_if_needed`` (Anthropic→OpenAI body
  translation, only meaningful for /v1/messages).
"""
from __future__ import annotations

from pathlib import Path


# ── structural pins ────────────────────────────────────────────────────


def test_handler_shared_module_exists():
    from app.api._handler_shared import (  # noqa: F401
        prepare_request_context,
        normalize_request_body,
    )


def test_messages_handler_imports_from_shared():
    """messages.py uses _handler_shared.prepare_request_context, NOT
    the old _messages_pre_route version."""
    src = Path("app/api/messages.py").read_text()
    assert "from app.api._handler_shared import prepare_request_context" in src
    assert "from app.api._handler_shared import normalize_request_body" in src


def test_completions_handler_imports_from_shared():
    """completions.py uses the SAME shared helpers as messages.py."""
    src = Path("app/api/completions.py").read_text()
    assert "from app.api._handler_shared import prepare_request_context" in src
    assert "from app.api._handler_shared import normalize_request_body" in src


def test_endpoint_kwarg_required_and_distinct():
    """The shared helper requires an ``endpoint`` kwarg, and the two
    handlers pass distinct values — this is what keeps Prometheus
    counters + LLM-stop scoping correct."""
    import inspect
    from app.api._handler_shared import prepare_request_context
    sig = inspect.signature(prepare_request_context)
    assert "endpoint" in sig.parameters
    # Keyword-only
    assert sig.parameters["endpoint"].kind == inspect.Parameter.KEYWORD_ONLY

    msg_src = Path("app/api/messages.py").read_text()
    com_src = Path("app/api/completions.py").read_text()
    assert 'endpoint="messages"' in msg_src
    assert 'endpoint="completions"' in com_src


def test_normalize_request_body_takes_endpoint():
    """Same endpoint parameterization for the validation helper —
    validate_completion_request itself dispatches on this arg."""
    import inspect
    from app.api._handler_shared import normalize_request_body
    sig = inspect.signature(normalize_request_body)
    assert "endpoint" in sig.parameters
    assert sig.parameters["endpoint"].kind == inspect.Parameter.KEYWORD_ONLY


def test_inline_blocks_gone_from_completions_py():
    """The version-comment markers that delimited the extracted
    blocks are gone from completions.py — they live in the helper now."""
    src = Path("app/api/completions.py").read_text()
    # v3.0.45 tenant ctx block — was the start of the extracted sub-block
    assert "v3.0.45: tenant context for ownership filter" not in src
    # v4.4.15 telemetry block
    assert "v4.4.15 (F-OBS-003) — caller-memory gating-header visibility" not in src
    # v4.4.23 contextvar block
    assert "v4.4.23 — per-request header-presence contextvars" not in src


def test_completions_py_dropped_to_894_or_less():
    """Phase 2 trims completions.py. Pre-refactor was 931 LOC;
    sub-block-1 + sub-block-2 extracts drop it to ~894. Setting the
    pin at 900 gives a small buffer for in-place comment edits."""
    src = Path("app/api/completions.py").read_text()
    n = src.count("\n") + 1
    assert n <= 900, (
        f"v5.7.23: completions.py is {n} LOC; expected <= 900 after "
        f"Phase 2 sub-block 1 + 2 extracts."
    )


def test_handler_shared_has_no_translation_helper():
    """``translate_to_openai_if_needed`` is messages-specific and
    stays in _messages_pre_route. Putting it in _handler_shared would
    introduce endpoint-specific logic in a shared module. (Module
    docstring may MENTION the name; we check for the actual ``def``.)"""
    src = Path("app/api/_handler_shared.py").read_text()
    assert "def translate_to_openai_if_needed" not in src


def test_translate_helper_stays_in_messages_pre_route():
    """Phase 1 sub-block 3 (the v3.10.0 widened translation block)
    still lives in _messages_pre_route — confirmed by source-grep."""
    src = Path("app/api/_messages_pre_route.py").read_text()
    assert "def translate_to_openai_if_needed" in src


def test_version_bumped():
    """v5.7.23 minimum — later patches keep this passing."""
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (5, 7, 23), f"v5.7.23 must be reachable; got {__version__}"
