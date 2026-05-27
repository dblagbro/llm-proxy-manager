"""v4.4.19 — ARCH-A pool-trace slicing direction fix.

Surfaced 2026-05-26 by the resumed ARCH-A dig: the tracer caught a real
59-hour stuck checkout (so the leak is reproducing), but the captured
stack was *all SQLAlchemy internals* with zero app frames — so the
leaking codepath couldn't be identified.

Root cause: ``traceback.format_stack()`` returns
**outermost-first / innermost-last**. The hook sliced ``[-45:]``,
keeping only the innermost 45 frames = the SQLA pool-checkout chain +
the hook itself. The app caller (the part that names the leaking
codepath) lives in the **outer** frames, which is exactly what the
slice discarded.

Both v3.10.13 (which bumped 18→45 claiming "reliably includes app
code") and the original ``[-18:]`` had the slicing direction wrong.

Fix: keep the full stack, dropping only the trivial trailing
``format_stack`` frame: ``[:-1]``.

These tests exercise the captured-stack shape (NOT the live pool —
that would require flipping ``DB_POOL_TRACE`` on for the test suite,
which has overhead and global-state issues).
"""
from __future__ import annotations

from pathlib import Path
import traceback


def test_source_no_longer_slices_innermost():
    """Guard: the ``[-45:]`` pattern must not return. Any future
    re-introduction of that slice would silently regress the leak hunt."""
    src = Path("app/models/database.py").read_text()
    assert "format_stack()[-45:]" not in src, (
        "format_stack()[-45:] keeps SQLA-internal frames and discards "
        "the app caller — regressing ARCH-A leak hunting"
    )
    assert "format_stack()[-18:]" not in src, "same direction bug at width 18"
    # The fix shape — drop only the trailing format_stack frame.
    assert "format_stack()[:-1]" in src, (
        "expected the v4.4.19 fix ``format_stack()[:-1]`` to be in place"
    )


def test_format_stack_direction_assumption():
    """Document + lock in the direction of ``format_stack()``.

    The fix is correct ONLY if ``format_stack()`` returns frames in
    outermost-first / innermost-last order. If a future Python version
    ever reversed that, the slice direction would need to flip too.
    This test asserts the assumption directly."""

    def inner():
        return traceback.format_stack()

    def outer():
        return inner()

    frames = outer()
    # Outermost = test runner (this file's caller); innermost = the
    # call to format_stack() inside ``inner``. So:
    # - the LAST frame should reference ``inner`` (where format_stack ran)
    # - some EARLIER frame should reference ``outer`` (the caller)
    assert "inner" in frames[-1], (
        f"expected innermost frame to mention 'inner', got: {frames[-1]!r}"
    )
    # ``outer`` should appear earlier in the list than ``inner``
    outer_idx = next((i for i, f in enumerate(frames) if "in outer" in f), -1)
    inner_idx = next((i for i, f in enumerate(frames) if "in inner" in f), -1)
    assert outer_idx >= 0 and inner_idx >= 0, "frames missing helpers"
    assert outer_idx < inner_idx, (
        "format_stack() ordering reversed from outermost-first — the "
        "v4.4.19 slice direction assumption is now wrong"
    )


def test_slice_keeps_outer_frames():
    """Exercise the chosen slice shape on a synthetic deep stack and
    verify the outer-most (app-equivalent) frame survives."""

    def app_caller_marker():
        # Synthetic stand-in for the application code that called
        # `await db.execute(...)`. Under the BUG this frame was lost.
        return _level_a()

    def _level_a(): return _level_b()
    def _level_b(): return _level_c()
    def _level_c(): return _level_d()
    def _level_d(): return _level_e()
    def _level_e(): return _level_f()
    def _level_f(): return _level_g()
    def _level_g(): return _level_h()
    def _level_h(): return _level_i()
    def _level_i(): return _level_j()
    def _level_j(): return _capture()

    def _capture():
        # Mimics the hook: keep all frames except the trivial last one.
        return "".join(traceback.format_stack()[:-1])

    stack = app_caller_marker()
    assert "app_caller_marker" in stack, (
        "v4.4.19 slice lost the outermost (app-caller) frame — "
        "the leak-hunt regression is back"
    )
    # The OLD bug would discard this; the FIX keeps it. Sanity-check
    # the old behavior on the same synthetic stack to make the
    # regression risk concrete:
    def _capture_bug():
        return "".join(traceback.format_stack()[-3:])

    def _re_app():
        return _re_a()

    def _re_a(): return _re_b()
    def _re_b(): return _capture_bug()

    bug_stack = _re_app()
    assert "_re_app" not in bug_stack, (
        "control case: slicing [-3:] would have kept the outer frame "
        "— but it shouldn't, that's the bug shape we're avoiding"
    )
