"""v5.7.18 (refactor) — messages.py sub-block 1 extracted to
``_messages_pre_route.prepare_request_context``.

Behavior-preserving. The extract collapses the pre-body-parse setup
(verify key + tenant ctx + compliance UA + LLM emergency stop +
telemetry) from ~50 lines inline into a single helper call. Future
sub-blocks (#2 normalize_request_body, #3 adapt_wire_format) ship
as separate v5.7.x patches.
"""
from __future__ import annotations

from pathlib import Path


# ── structural pins ────────────────────────────────────────────────────


def test_helper_module_exists():
    from app.api._messages_pre_route import prepare_request_context  # noqa: F401


def test_messages_handler_uses_helper():
    """messages.py imports and calls prepare_request_context. Source
    grep — if anyone re-inlines this block, this test catches the
    regression. (v5.7.23: import path moved to _handler_shared.)"""
    src = Path("app/api/messages.py").read_text()
    assert "from app.api._handler_shared import prepare_request_context" in src
    assert "key_record = await prepare_request_context(" in src


def test_helper_signature_kwargs_match_call_site():
    """The helper accepts the two keyword args the caller passes —
    catches any drift where messages.py is updated but the helper
    isn't, or vice versa."""
    import inspect
    from app.api._messages_pre_route import prepare_request_context
    sig = inspect.signature(prepare_request_context)
    assert "x_conversation_id" in sig.parameters
    assert "x_memory_tag" in sig.parameters
    # The first three positional args are request, db, x_api_key.
    positional = [
        n for n, p in sig.parameters.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                      inspect.Parameter.POSITIONAL_ONLY)
    ]
    assert positional[:3] == ["request", "db", "x_api_key"], positional


def test_inline_block_is_gone_from_messages_py():
    """Pin: the version comments that delimited the extracted block
    no longer appear at module-top inline scope. Specifically the
    v4.4.15 telemetry block AND the v4.4.23 contextvar block live in
    ``_messages_pre_route.py`` now — finding them at line < 200 of
    messages.py means someone re-inlined the extract."""
    src = Path("app/api/messages.py").read_text()
    inline_marker = "v4.4.15 (F-OBS-003) — record whether the caller-memory gating"
    idx = src.find(inline_marker)
    # The marker now lives only in _messages_pre_route. If it shows up
    # in messages.py at all (idx != -1), that's the regression.
    assert idx == -1, (
        "v5.7.18: pre-route block was re-inlined into messages.py — "
        "refactor regression. The block belongs in _messages_pre_route."
    )


def test_messages_py_size_dropped():
    """The whole point of the refactor is messages.py getting smaller.
    Pre-5.7.18 the file was 1180 LOC; the sub-block-1 extract should
    drop it to <= 1140 (about 40 LOC removed). If it grew back, a new
    sub-block was added without an extract — surface it here."""
    src = Path("app/api/messages.py").read_text()
    n = src.count("\n") + 1
    assert n <= 1145, (
        f"v5.7.18: messages.py is {n} LOC; expected <= 1145 after the "
        f"sub-block-1 extract. New code added? Extract a sub-block first."
    )


def test_version_bumped():
    """v5.7.18 minimum — later patches keep this passing."""
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (5, 7, 18), f"v5.7.18 must be reachable; got {__version__}"
