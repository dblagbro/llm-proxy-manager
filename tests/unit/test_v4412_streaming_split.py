"""v4.4.12 _messages_streaming.py refactor — invariants for the split.

Pre-split ``app/api/_messages_streaming.py`` was 979 LOC and on the
watch list as the next refactor candidate (per the post-v4.4.11
file-size analysis). v4.4.12 extracts the claude-oauth dispatch
section into its own sibling module, keeping the parent file
focused on the litellm-backed Anthropic-streaming path + helpers.

This test file guards three invariants:

1. **Both files load cleanly** — basic smoke.
2. **The re-export shim is complete** — every claude-oauth symbol that
   was importable from ``_messages_streaming`` pre-split is still
   importable from there post-split (back-compat for the 4+ external
   callers).
3. **Neither file exceeds 700 LOC** — soft ceiling for the post-split
   files. If either grows past 700, signal "re-split this domain".
"""
from __future__ import annotations

from pathlib import Path
import importlib


def test_both_streaming_modules_load_cleanly():
    importlib.import_module("app.api._messages_streaming")
    importlib.import_module("app.api._messages_streaming_oauth")


def test_oauth_symbols_still_importable_from_parent_module():
    """The split is back-compat: existing
    ``from app.api._messages_streaming import _stream_claude_oauth, ...``
    imports must keep working unchanged."""
    from app.api._messages_streaming import (  # noqa: F401
        _inject_claude_code_system,
        _count_cache_control_markers,
        _prepare_claude_oauth_request,
        _oauth_complete_timeout,
        _refresh_oauth_token,
        _complete_claude_oauth,
        _stream_claude_oauth,
        _CLAUDE_CODE_SYS_MARKER,
        _ALLOWED_SYS_MARKERS,
        _CLAUDE_OAUTH_TIMEOUT,
        _CLAUDE_OAUTH_STREAM_TIMEOUT,
    )


def test_litellm_path_still_lives_in_parent_module():
    """``_stream_anthropic`` (the litellm-backed translator) and its
    sibling functions stay in the original file — only claude-oauth
    moved."""
    src = Path("app/api/_messages_streaming.py").read_text()
    assert "async def _stream_anthropic(" in src
    assert "async def _stream_cot_anthropic(" in src
    assert "async def _webhook_completion_anthropic(" in src


def test_oauth_path_lives_in_new_module():
    """``_stream_claude_oauth`` + ``_complete_claude_oauth`` + the
    Claude-Code-marker injector all moved to the new module."""
    src = Path("app/api/_messages_streaming_oauth.py").read_text()
    assert "async def _stream_claude_oauth(" in src
    assert "async def _complete_claude_oauth(" in src
    assert "def _inject_claude_code_system(" in src


def test_exc_str_duplicate_is_identical():
    """The split duplicates ``_exc_str`` into the oauth module to avoid
    a circular import via the re-export shim. If one copy drifts from
    the other, error-string handling will differ between the two
    streaming paths — silently. This test pins them as identical."""
    from app.api._messages_streaming import _exc_str as parent_exc_str
    from app.api._messages_streaming_oauth import _exc_str as oauth_exc_str

    # Compare behavior across an exception with a message, without one,
    # and a custom subclass — covers the BUG-008-class flavor of error
    # strings that motivated this helper.
    class _Custom(Exception):
        pass

    cases = [
        Exception("boom"),
        Exception(""),
        Exception(),
        _Custom(),
        _Custom("with msg"),
        TimeoutError(),
    ]
    for e in cases:
        assert parent_exc_str(e) == oauth_exc_str(e), \
            f"_exc_str copies drifted on {type(e).__name__}: " \
            f"parent={parent_exc_str(e)!r}, oauth={oauth_exc_str(e)!r}"


def test_neither_streaming_file_exceeds_700_loc():
    """Soft ceiling per split file. If either crosses 700 LOC, signal
    "re-split this domain" — pre-split was 979 in one file; the split
    target was to keep both children well under that mass."""
    too_big = []
    for fn in (
        "app/api/_messages_streaming.py",
        "app/api/_messages_streaming_oauth.py",
    ):
        loc = sum(1 for _ in Path(fn).read_text().splitlines())
        if loc > 700:
            too_big.append((fn, loc))
    assert not too_big, (
        f"these post-split files now exceed 700 LOC: {too_big}. "
        f"Time to split further."
    )


def test_external_callers_still_resolve_oauth_symbols():
    """Spot-check the 4 known external import sites still resolve."""
    # app/providers/scanner.py uses _inject_claude_code_system
    from app.api._messages_streaming import _inject_claude_code_system  # noqa: F401
    # app/routing/hedging.py uses _sse_frame_error (stays in parent)
    from app.api._messages_streaming import _sse_frame_error  # noqa: F401
    # app/api/completions.py uses preflight_sse + http_status_for_stream_error
    from app.api._messages_streaming import (  # noqa: F401
        preflight_sse, http_status_for_stream_error,
    )
    # app/api/_messages_dispatch.py uses _stream_claude_oauth + _complete_claude_oauth
    from app.api._messages_streaming import (  # noqa: F401
        _stream_claude_oauth, _complete_claude_oauth,
    )
    # app/monitoring/keepalive.py uses _complete_claude_oauth (already covered)
