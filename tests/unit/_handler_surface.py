"""Source-text view of a handler's whole surface, across extraction seams.

v5.22.37 — ``app/api/messages.py`` was 1409 LOC and three pins
(``test_v5190_``, ``test_v5718_``, ``test_v5719_``) carried successively lower
targets that nothing met. Extracting the handler's two dispatch arms into
``_messages_response_dispatch.py`` got it to 933 — and orphaned **thirteen**
source-grep guards in one move, because each read
``Path("app/api/messages.py").read_text()`` and asserted that some call was
present in it.

That is the BUG-091 failure mode at scale: an extraction silently invalidates
every guard pointed at the vacated file, and the suite reports it as N
unrelated failures rather than one cause. This module is the fix for the
*next* extraction as much as this one — a guard that asks for "the messages
handler surface" keeps working when the code moves again, and only this tuple
needs updating.

Use ``messages_handler_source()`` when asserting that something **is** wired
into the handler. Keep reading ``messages.py`` directly when asserting
something is **absent** from it specifically — e.g.
``test_v5190_...::test_messages_inline_blocks_removed``, whose whole point is
that a block no longer sits in that file.
"""
from __future__ import annotations

from pathlib import Path

# messages.py plus every module its handler body has been extracted into.
MESSAGES_SURFACE_FILES = (
    "app/api/messages.py",
    "app/api/_messages_response_dispatch.py",
    # v5.22.37 — the non-streaming arm split again, out of the module above, to
    # stay clear of the 700-LOC ceiling. Adding it here was the whole point of
    # this helper: one line, instead of repointing 14 guards by hand again.
    "app/api/_messages_nonstream_dispatch.py",
)

# completions.py likewise. v5.22.37 split its two own-dispatcher branches out to
# meet the 900-LOC pin in test_v5723_phase2_shared_helpers.
COMPLETIONS_SURFACE_FILES = (
    "app/api/completions.py",
    "app/api/_completions_claude_oauth.py",
    "app/api/_completions_grok_web.py",
)


def _read_all(files) -> str:
    return "\n".join(Path(f).read_text() for f in files)


def messages_handler_source() -> str:
    """Concatenated source of the /v1/messages handler surface."""
    return _read_all(MESSAGES_SURFACE_FILES)


def completions_handler_source() -> str:
    """Concatenated source of the /v1/chat/completions handler surface."""
    return _read_all(COMPLETIONS_SURFACE_FILES)


def handler_source(endpoint: str) -> str:
    """Surface for ``"messages"`` or ``"completions"``.

    Useful where a test is parametrized over both endpoint modules.
    """
    if endpoint in ("messages", "app/api/messages.py"):
        return messages_handler_source()
    if endpoint in ("completions", "app/api/completions.py"):
        return completions_handler_source()
    raise ValueError(f"unknown endpoint surface: {endpoint!r}")
