"""v5.22.37 — LOC ceilings for the modules the handler splits created.

The v5.22.26-37 arc emptied ``known_failures.txt``, and its last six entries
were LOC-ceiling pins. Four files came under their ceilings by moving code into
six new modules. Nothing watched those, which is the failure mode the
``_sse_guard.py`` addition to ``test_v4412_streaming_split`` called out in
v5.22.36: **a split that satisfies a ceiling by moving mass into an unwatched
sibling has not reduced anything.** This closes that loop for the rest.

Ceiling is 700, matching the enforced figure for the streaming domain in
``test_v4412_streaming_split``. ``design.md`` rule 1 puts the *trigger to
consider splitting* at ~600, which is deliberately lower than the enforced
limit — so a file between 600 and 700 is a nudge, not a failure.

``_messages_response_dispatch.py`` is knowingly the closest to the line. It
holds the two /v1/messages dispatch arms, and the non-streaming one is roughly
twice the streaming one; if this file needs to grow, split that arm out rather
than raising the number here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

CEILING = 700

# Modules created by the v5.22.36/37 handler + domain splits.
SPLIT_MODULES = (
    "app/api/_sse_guard.py",
    "app/api/_messages_response_dispatch.py",
    "app/api/_messages_nonstream_dispatch.py",
    "app/api/_completions_claude_oauth.py",
    "app/api/_completions_grok_web.py",
    "app/models/db_model.py",
)


@pytest.mark.parametrize("path", SPLIT_MODULES)
def test_split_module_exists(path):
    assert Path(path).is_file(), (
        f"{path} is missing — if it was merged back or renamed, update "
        f"SPLIT_MODULES and tests/unit/_handler_surface.py together"
    )


@pytest.mark.parametrize("path", SPLIT_MODULES)
def test_split_module_under_ceiling(path):
    loc = len(Path(path).read_text().splitlines())
    assert loc <= CEILING, (
        f"{path} is {loc} LOC, over the {CEILING} ceiling. Split it rather than "
        f"raising this number — see design.md rule 1."
    )


def test_handler_surface_helper_lists_every_split_module():
    """The surface helper must know about every module a handler body moved
    into, or source-grep guards silently stop seeing the code.

    This is the guard against repeating v5.22.37's own mistake: that extraction
    orphaned 13 guards on messages.py and 8 on completions.py, each of which had
    to be repointed by hand.
    """
    from tests.unit._handler_surface import (
        COMPLETIONS_SURFACE_FILES,
        MESSAGES_SURFACE_FILES,
    )

    known = set(MESSAGES_SURFACE_FILES) | set(COMPLETIONS_SURFACE_FILES)
    handler_splits = {
        "app/api/_messages_response_dispatch.py",
        "app/api/_messages_nonstream_dispatch.py",
        "app/api/_completions_claude_oauth.py",
        "app/api/_completions_grok_web.py",
    }
    missing = handler_splits - known
    assert not missing, (
        f"these handler-body modules are not in _handler_surface.py: "
        f"{sorted(missing)}. Source-grep guards read the surface through that "
        f"helper, so an omission makes them silently stop checking the code."
    )
