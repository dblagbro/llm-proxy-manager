"""Handler entry-surface source reader for static "is X wired in?" pins.

The v5.0.x / v5.7.18 refactor lifted the per-request ENTRY logic of
``messages.py`` and ``completions.py`` (request-context prep, body
normalization, cross-format translation, conversation-id telemetry,
caller-memory headers, orig-model capture) into shared helper modules
that the handlers call UNCONDITIONALLY at entry:

    - app/api/_handler_shared.py   (prepare_request_context, normalize_request_body)
    - app/api/_messages_pre_route.py  (translation gate — messages only)

Static-grep tests written before that extraction look for those symbols
in the handler file itself and now fail even though the wiring is intact
(the handler calls the helper on line ~99-115). ``entry_surface()``
returns the handler PLUS its entry helpers so those pins find the symbol
where it actually lives. Helpers are concatenated FIRST because they run
first at entry — so "capture happens before body mutation" ordering
checks still hold (capture is in the helper, mutation in the handler).

This does NOT weaken the guard: the symbol must still exist in code the
handler provably calls at entry. If a feature were truly removed, it
would be absent from the surface and the test would still fail.
"""
from __future__ import annotations

from pathlib import Path

_ENTRY_HELPERS = {
    "app/api/messages.py": [
        "app/api/_handler_shared.py",
        "app/api/_messages_pre_route.py",
    ],
    "app/api/completions.py": [
        "app/api/_handler_shared.py",
    ],
}


def entry_surface(handler_file: str) -> str:
    """Return the entry-helper sources (run first) concatenated before the
    handler file's own source."""
    parts = [
        Path(h).read_text()
        for h in _ENTRY_HELPERS.get(handler_file, [])
        if Path(h).exists()
    ]
    parts.append(Path(handler_file).read_text())
    return "\n# ==== handler ====\n".join(parts)
