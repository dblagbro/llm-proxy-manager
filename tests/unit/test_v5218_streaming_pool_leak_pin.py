"""v5.21.8 — CI pin against DB pool leaks from ``Depends(get_db)`` + ``StreamingResponse``.

The chronic 2026-07-09/14/15 outage was caused by a single endpoint
(``runs.py::get_events``) that held a request-scoped DB session across
a multi-hour StreamingResponse. v5.21.7 fixed it. This test pins the
class of failure so no future refactor silently reintroduces it.

Any endpoint that BOTH:
  1. Takes ``Depends(get_db)`` in its signature
  2. Returns a ``StreamingResponse``

...must document why it's safe by declaring the safety condition
in an inline comment near the signature: ``# pool-leak-audit: <reason>``.

Recognized reasons:
- ``pool-leak-audit: rows-materialized`` — DB rows loaded fully before
  the StreamingResponse is returned; the generator doesn't touch the DB.
  Session releases when the handler returns (before the stream starts
  yielding bytes). Example: ``compliance.admin_compliance_events``.
- ``pool-leak-audit: watchdog+bounded`` — response is a bounded LLM
  stream (~60s max) with the ``watch_for_disconnect`` dep wired. On
  client disconnect the handler is cancelled and the get_db finalizer
  releases the session. Example: ``messages.messages``,
  ``completions.chat_completions``.
- ``pool-leak-audit: exempt-<reason>`` — case-by-case exemption. Use
  sparingly with an operator-facing note in the surrounding code.

Any handler matching (1)+(2) WITHOUT a matching comment fails this
test. Runs at CI time.
"""
from __future__ import annotations
import re
from pathlib import Path


def _get_handler_files() -> list[Path]:
    """All files under app/api/ that register routes."""
    root = Path("app/api")
    files = []
    for p in sorted(root.rglob("*.py")):
        # Skip __pycache__ and known helper files that don't declare routes
        if "__pycache__" in p.parts:
            continue
        if p.name.startswith("_"):
            # Underscored files (e.g. ``_response_hook_runner.py``) are
            # helper modules that don't own routes.
            continue
        files.append(p)
    return files


_HANDLER_DECL_RE = re.compile(
    r"^@router\.(?:get|post|put|patch|delete)\(",
    re.MULTILINE,
)
_FUNC_NAME_RE = re.compile(r"\basync def (\w+)\s*\(")


def _iter_router_handlers(src: str):
    """Yield (func_name, full_handler_block, decorator_pos) for each
    @router-decorated async def in ``src``. Block extends from the
    decorator to the next decorator OR module-level def/class.

    Nested-paren-tolerant: unlike a regex approach, we walk the source
    linearly and use decorator-start positions as boundaries."""
    starts = [m.start() for m in _HANDLER_DECL_RE.finditer(src)]
    starts.append(len(src))  # sentinel
    for i in range(len(starts) - 1):
        block_start = starts[i]
        block_end = starts[i + 1]
        block = src[block_start:block_end]
        # Look also at the 6 lines IMMEDIATELY above the decorator so a
        # ``# pool-leak-audit:`` comment placed there gets picked up as
        # belonging to THIS handler. Kept separate from ``block`` so
        # the ``Depends(get_db)``/``StreamingResponse`` searches only
        # match inside this handler, not sibling code above.
        pre_start = block_start
        lines_back = 0
        prev_end = starts[i - 1] if i > 0 else 0
        while pre_start > prev_end and lines_back < 6:
            pre_start -= 1
            if src[pre_start] == "\n":
                lines_back += 1
        preamble = src[pre_start:block_start]
        m = _FUNC_NAME_RE.search(block)
        if not m:
            continue
        func_name = m.group(1)
        yield func_name, block, preamble, block_start


_SAFETY_COMMENT_RE = re.compile(
    r"pool-leak-audit:\s*(rows-materialized|watchdog\+bounded|exempt-[\w-]+)"
)


def test_no_undocumented_streaming_pool_leak_pattern():
    """Fail if any router handler has BOTH Depends(get_db) AND
    StreamingResponse WITHOUT a documented safety condition nearby."""
    offenders: list[tuple[str, str, int]] = []
    for f in _get_handler_files():
        src = f.read_text()
        for func_name, block, preamble, decorator_pos in _iter_router_handlers(src):
            # Match the dep-declaration form ``= Depends(get_db)`` — not
            # bare mentions in comments or docstrings, which are false
            # positives (a handler that DOCUMENTS its migration away
            # from the pattern shouldn't fail this pin).
            if "= Depends(get_db)" not in block:
                continue
            if "StreamingResponse(" not in block:
                continue
            # Safety comment can live in the handler body OR in the
            # ~6-line preamble immediately above the decorator.
            if _SAFETY_COMMENT_RE.search(block) or _SAFETY_COMMENT_RE.search(preamble):
                continue
            line = src[:decorator_pos].count("\n") + 1
            offenders.append((str(f), func_name, line))

    assert not offenders, (
        "The following handlers combine Depends(get_db) with StreamingResponse "
        "but do NOT document their pool-leak-safety condition. Add a comment "
        "of the form ``# pool-leak-audit: <reason>`` where <reason> is one of "
        "rows-materialized, watchdog+bounded, or exempt-<...>. See the docstring "
        "of tests/unit/test_v5218_streaming_pool_leak_pin.py for the audit spec.\n\n"
        + "\n".join(f"  - {f}:{ln}  {name}" for f, name, ln in offenders)
    )
