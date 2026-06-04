"""v5.0.12 — no ``event: budget`` SSE frame in any streaming generator.

Hub team filed a bug 2026-06-04: the mid-stream ``event: budget`` frame
the proxy used to emit between the last chat-completion chunk and
``data: [DONE]`` crashes every Vercel-AI-SDK consumer (OpenCode, Cursor
IDE, continue.dev) on a strict Zod ``{choices}|{error}`` discriminator
check. v5.0.12 removed the emission at all three sites:

  - app/api/_completions_streaming.py
  - app/api/_messages_streaming.py
  - app/api/_messages_streaming_oauth.py

Budget info remains on the ``X-Token-Budget-Remaining`` response
header set pre-stream. These static-pin tests catch any accidental
re-introduction.
"""
from __future__ import annotations

from pathlib import Path

import pytest

STREAMING_FILES = [
    "app/api/_completions_streaming.py",
    "app/api/_messages_streaming.py",
    "app/api/_messages_streaming_oauth.py",
]


@pytest.mark.parametrize("path", STREAMING_FILES)
def test_no_event_budget_frame_in_streaming_module(path):
    """No streaming generator may yield an ``event: budget`` SSE frame.

    Matches the actual emission syntax (the f-string / bytes literal /
    plain string forms that yield SSE bytes). Comments mentioning the
    removed frame are fine.
    """
    src = Path(path).read_text()
    bad = (
        "f'event: budget",   # f-string single-quote (pre-v5.0.12 form)
        'f"event: budget',   # f-string double-quote
        "'event: budget",    # plain string single-quote yield
        '"event: budget',    # plain string double-quote yield
        "b'event: budget",   # raw bytes single-quote
        'b"event: budget',   # raw bytes double-quote
    )
    for marker in bad:
        assert marker not in src, (
            f"{path}: re-introduced ``event: budget`` SSE frame "
            f"(matched {marker!r}). This crashes Vercel-AI-SDK consumers "
            "(Zod {choices}|{error} discriminator rejects it). Use the "
            "X-Token-Budget-Remaining response header instead, or fold "
            "into the final usage chunk."
        )


def test_x_token_budget_header_still_present():
    """v5.0.12 dropped the SSE frame but kept the header path as the
    canonical budget-signaling surface. If THIS goes away too, callers
    lose the budget signal entirely."""
    paths = (
        "app/api/_request_pipeline.py",
        "app/api/_messages_dispatch.py",
        "app/api/completions.py",
        "app/api/messages.py",
    )
    for p in paths:
        src = Path(p).read_text()
        assert "X-Token-Budget-Remaining" in src, (
            f"{p}: lost the X-Token-Budget-Remaining header — that's the "
            "remaining budget surface after v5.0.12 dropped the SSE frame."
        )
