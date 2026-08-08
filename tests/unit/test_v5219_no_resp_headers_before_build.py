"""v5.21.9 hotfix — pin against ``resp_headers`` used before it's built.

Class of bug: the messages/completions handlers build ``resp_headers``
via ``build_base_response_headers(...)`` at a specific point in the
handler flow (after route selection). Any code path that references
``resp_headers`` BEFORE that build line raises ``UnboundLocalError:
cannot access local variable 'resp_headers'`` and 500s the request.

This bug shipped in v5.21.6 (my buffered-cascade-mode extraction called
``detect_buffered_cascade_mode(..., resp_headers)`` at line 122 while
``resp_headers`` was built at line 476) and 500-ed EVERY /v1/messages
call until 2026-07-16 when it was caught by log inspection. v5.21.9
fixes the specific bug + this test pins the class.

Also related: v5.21.1 hotfix for a similar local-name-shadowing bug.
Both shipped from refactors that failed to run any test hitting the
actual endpoint.
"""
from __future__ import annotations
import ast
from pathlib import Path


def _check_file(path: Path) -> list[str]:
    """Return a list of ``func_name @ line`` where ``resp_headers`` is
    referenced BEFORE its assignment (via
    ``resp_headers = build_base_response_headers(...)``) in the function
    body. Empty list = safe.
    """
    src = path.read_text()
    tree = ast.parse(src)
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Walk the function body in order. Track when resp_headers is
        # assigned. Any Name.load of resp_headers before that is bad.
        assigned_line: int | None = None
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "resp_headers":
                        if assigned_line is None or sub.lineno < assigned_line:
                            assigned_line = sub.lineno
        if assigned_line is None:
            continue  # never assigned in this function; not our concern
        # Now walk again and flag any Name Load of resp_headers before that
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id == "resp_headers" \
                    and isinstance(sub.ctx, ast.Load) \
                    and sub.lineno < assigned_line:
                offenders.append(f"{node.name} @ line {sub.lineno} (assigned at {assigned_line})")
                break  # one report per function is enough

    return offenders


def test_messages_no_resp_headers_before_build():
    offenders = _check_file(Path("app/api/messages.py"))
    assert not offenders, (
        "resp_headers referenced BEFORE its build in app/api/messages.py:\n  "
        + "\n  ".join(offenders)
    )


def test_completions_no_resp_headers_before_build():
    offenders = _check_file(Path("app/api/completions.py"))
    assert not offenders, (
        "resp_headers referenced BEFORE its build in app/api/completions.py:\n  "
        + "\n  ".join(offenders)
    )


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 21, 9), (
        f"expected >= 5.21.9, got {major}.{minor}.{patch}"
    )
