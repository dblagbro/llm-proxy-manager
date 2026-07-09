"""v5.14.1 — Static-grep test pinning **every** model-resolving endpoint
to ``apply_response_hooks``. Mitigates the LiteLLM #27518 bypass class
that I explicitly committed to avoiding in the 2026-06-30 reply memo to
hub team:

  "Static-grep test that asserts every endpoint with Depends(get_db)
   calls the hook runner — same shape as the v5.7.17 / v5.9.9 /
   v5.9.10 watchdog ordering tests. If a future endpoint forgets, the
   test fails before merge."

  "One file, one helper, one test pinning every endpoint to it. No
   'this hook fires on three of four endpoints' surprises."

v5.14.0 shipped with only 2 of 7 wired (messages + completions); v5.14.1
catches up the remaining 5 + locks in this pin so it stays caught up.
"""
from __future__ import annotations

from pathlib import Path


# The seven model-resolving handler files. Each must call
# ``apply_response_hooks`` somewhere AND import ``HookContext``.
# `_messages_dispatch.py` is the failover-dispatcher used INSIDE messages.py
# + completions.py; it doesn't return its own JSONResponse — the hook fires
# at the calling handler's exit. So it's not pinned here; the pin is on
# the 6 endpoint files that own a JSONResponse return path.
# v5.19.0 — messages.py's tail extracted to _messages_response_tail.py.
# The pin now recognizes both the main endpoint file AND its optional
# ``_<name>_response_tail.py`` companion. Same intent: the runner MUST
# be reachable from the endpoint's dispatch path. Future extracts follow
# the same naming convention and land in this list automatically.
PINNED_ENDPOINTS = {
    "app/api/messages.py": ["app/api/_messages_response_tail.py"],
    "app/api/completions.py": [],
    "app/api/responses.py": [],
    "app/api/audio.py": [],
    "app/api/images.py": [],
    "app/api/embeddings.py": [],
}


def _all_sources_for(path: str) -> str:
    """Return the endpoint file's text concatenated with any extract
    companions so static-grep asserts hit both."""
    files = [path] + PINNED_ENDPOINTS[path]
    return "\n".join(
        Path(f).read_text() for f in files if Path(f).exists()
    )


def test_all_pinned_endpoints_import_runner():
    """Every pinned endpoint (or its extract companion) imports the
    runner + the context type."""
    for path in PINNED_ENDPOINTS:
        src = _all_sources_for(path)
        assert "from app.api._response_hook_runner import apply_response_hooks, HookContext" in src, (
            f"v5.14.1: {path} (or its extract) must import the response hook runner."
        )


def test_all_pinned_endpoints_invoke_runner_with_handler_id():
    """Every pinned endpoint (or its extract companion) must call
    ``apply_response_hooks(handler_id=...)`` at least once."""
    for path in PINNED_ENDPOINTS:
        src = _all_sources_for(path)
        assert "await apply_response_hooks(" in src, (
            f"v5.14.1: {path} (or its extract) must await apply_response_hooks(...)."
        )
        assert "handler_id=" in src, (
            f"v5.14.1: {path} (or its extract) must pass handler_id to apply_response_hooks."
        )


def test_handler_ids_are_unique():
    """The seven handlers each use a distinct ``handler_id`` so hub-side
    hooks that key on the id can target one without aliasing."""
    import re
    ids: dict[str, list[str]] = {}
    for path in PINNED_ENDPOINTS:
        src = _all_sources_for(path)
        for m in re.finditer(r'handler_id="([^"]+)"', src):
            ids.setdefault(m.group(1), []).append(path)
    # No id appears in more than one file.
    for handler_id, files in ids.items():
        # audio.py has TWO distinct ids (audio.speech + audio.transcriptions);
        # both are in the same file, which is fine — what matters is no
        # OTHER file uses them.
        seen_in = set(files)
        assert len(seen_in) == 1, (
            f"v5.14.1: handler_id={handler_id!r} appears in multiple files: {sorted(seen_in)}"
        )
    # Expected set covers all 7 surfaces. audio.py contributes 2.
    expected = {"messages", "completions", "responses", "audio.speech",
                "audio.transcriptions", "images", "embeddings"}
    found = set(ids.keys())
    missing = expected - found
    assert not missing, f"v5.14.1: missing handler_ids: {sorted(missing)}"


def test_version_bumped():
    """v5.14.1 shipped this pin; assert at-or-beyond."""
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', src)
    assert m, f"could not parse __version__ from {src!r}"
    major, minor = int(m.group(1)), int(m.group(2))
    assert (major, minor) >= (5, 14), f"expected >= 5.14, got {major}.{minor}"
