"""v5.7.2 hotfix — SPA catch-all must not swallow ``/mcp`` requests.

Surfaced during the v5.7.1 fleet roll: a bare ``GET /mcp`` (no
trailing slash) returned HTTP 200 + the React SPA index.html, not
the JSON 401 the MCP middleware was supposed to send.

Root cause: Starlette mounts require the path to end in the mount
prefix + ``/`` (or have additional path). ``/mcp`` (no slash) doesn't
match ``app.mount("/mcp", ...)``, so the SPA catch-all picked it up
and served the HTML shell. The actual ``/mcp/`` endpoint (with slash)
worked correctly — this was a UX bug for API clients probing without
the slash, not a security hole (the SPA HTML doesn't leak data).

Fix: extend the API-namespace skip list in the SPA catch-all to
include ``mcp``. Bare ``/mcp`` now returns JSON 404; ``/mcp/`` still
hits the FastMCP Streamable HTTP endpoint with bearer-key auth.
"""
from __future__ import annotations

from pathlib import Path


def test_spa_catchall_skips_mcp_namespace():
    """Source-grep contract: the catch-all's API-namespace
    short-circuit must list ``mcp`` alongside ``v1`` / ``api`` /
    ``cluster`` / ``lmrh`` / ``metrics`` / ``health`` / ``version``.
    """
    # v5.22.34 — was `window = src[idx:idx + 1500]` plus substring checks.
    # The handler's inline comments have since grown past 1500 chars, so the
    # window truncated mid-tuple and reported `"health"` and `"version"`
    # missing while both sat on the very next line. Parked in
    # known_failures.txt as a result.
    #
    # Now: read the namespaces out of the `head in (...)` tuples with the AST.
    # Sharper as well as staleness-proof — a substring check passes if the
    # namespace merely appears in a comment, this requires it to be a live
    # member of the tuple the handler actually tests.
    import ast

    src = Path("app/main.py").read_text()
    fn = next(
        (n for n in ast.walk(ast.parse(src))
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
         and n.name == "spa_catch_all"),
        None,
    )
    assert fn is not None, "SPA catch-all handler missing"

    def _in_tuples(node):
        """Every string literal on the right of an `x in (...)` inside `node`."""
        found = []
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Compare) or len(sub.ops) != 1:
                continue
            if not isinstance(sub.ops[0], ast.In):
                continue
            rhs = sub.comparators[0]
            if isinstance(rhs, (ast.Tuple, ast.List, ast.Set)):
                found.append({
                    e.value for e in rhs.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                })
        return found

    tuples = _in_tuples(fn)
    skipped = set().union(*tuples) if tuples else set()

    # Must include mcp in the API-namespace check
    assert "mcp" in skipped, (
        "SPA catch-all must skip the /mcp namespace; otherwise bare "
        "/mcp (no trailing slash) returns the SPA HTML instead of "
        "JSON 404 — confuses API clients probing the MCP endpoint."
    )
    # Belt-and-braces: every prior namespace must still be there
    for ns in ("v1", "api", "cluster", "lmrh", "metrics", "health", "version"):
        assert ns in skipped, (
            f"SPA catch-all skip list missing prior namespace {ns!r}; "
            f"found {sorted(skipped)}"
        )
    # v1/api/mcp are refused unconditionally, not only when a sub-path is
    # present — keep them together in one tuple so that stays true.
    assert any({"v1", "api", "mcp"} <= grp for grp in tuples), (
        "v1/api/mcp must share the unconditional short-circuit; splitting "
        'them into the `and "/" in full_path` branch would let a bare '
        "/mcp serve the SPA shell again (the v5.7.2 bug)"
    )
