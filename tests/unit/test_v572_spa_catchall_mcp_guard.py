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
    src = Path("app/main.py").read_text()
    # Find the catch-all handler body
    idx = src.find("async def spa_catch_all")
    assert idx != -1, "SPA catch-all handler missing"
    window = src[idx: idx + 1500]
    # Must include mcp in the API-namespace check
    assert '"mcp"' in window, (
        "SPA catch-all must skip the /mcp namespace; otherwise bare "
        "/mcp (no trailing slash) returns the SPA HTML instead of "
        "JSON 404 — confuses API clients probing the MCP endpoint."
    )
    # Belt-and-braces: every prior namespace must still be there
    for ns in ("v1", "api", "cluster", "lmrh", "metrics", "health", "version"):
        assert f'"{ns}"' in window, (
            f"SPA catch-all skip list missing prior namespace {ns!r}"
        )
