"""v5.7.0 — MCP (Model Context Protocol) aggregation endpoint.

Operator-approved 2026-06-14. Exposes a single Streamable HTTP MCP
endpoint at ``/mcp`` that downstream clients (Claude Code, opencode,
Cursor, Continue) can add as ONE MCP server URL to get N aggregated
capabilities. v5.7.0 ships with in-process tools only; v5.7.1 will
add external stdio sub-servers via FastMCP ``mount()``.

Architecture (from the 2026-06-14 research dive):
- FastMCP root server, ``stateless_http=True, json_response=True``.
  No sticky sessions; horizontally scalable; matches where the
  2026-07-28 spec is going.
- Mounted on the FastAPI app via ``app.mount("/mcp", ...)``. The
  ``streamable_http_path="/"`` keyword on FastMCP avoids the
  trailing-slash 307→404 trap (SDK issue #1367).
- Per-call audit row in a new ``mcp_tool_calls`` table linking to the
  parent LLM request id. v5.7.x will migrate the audit data into
  ``compliance_events`` for cluster replication.

Pinned SDK: ``mcp>=1.27,<2``. v5.8.0 will branch for the 2026-07-28
spec drop (session removal, new headers).
"""
from __future__ import annotations

import contextvars

# v5.7.0 — per-request context for the auth'd API key id. Tool
# implementations read this via ``current_api_key_id.get()`` so audit
# rows correctly attribute the call. Set by the bearer-auth
# middleware; default ``None`` means an unauthenticated request
# (rejected at the middleware layer before tools run).
current_api_key_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_current_api_key_id", default=None,
)

# Per-request context for the parent LLM request id, if the MCP call
# was nested inside a /v1/messages request that injected this context.
# v5.7.0 leaves this NULL because the MCP endpoint is reached
# directly from the client; v5.7.x may correlate via X-Parent-Request-Id.
current_parent_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_current_parent_request_id", default=None,
)
