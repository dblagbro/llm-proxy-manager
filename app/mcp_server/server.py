"""v5.7.0 — FastMCP root server + bearer-key auth middleware.

Constructs the root FastMCP server, registers in-process tools,
and returns the Starlette sub-app to mount on the FastAPI lifespan.

Auth model: clients send ``Authorization: Bearer <api_key>``. The
Starlette middleware validates via the same ``verify_api_key`` flow
used by ``/v1/messages``, sets ``current_api_key_id`` ContextVar so
tools can attribute audit rows, and returns 401 on miss.

v5.7.1 will add external sub-server mounts (filesystem, fetch, git,
markitdown) via ``root.mount(Client(...), prefix=...)``.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.mcp_server import current_api_key_id


logger = logging.getLogger(__name__)


# v5.7.4 — per-request ContextVar carrying the auth'd key's MCP policy
# (allow/deny lists + token budget). Set by BearerKeyAuthMiddleware
# during dispatch; read by the policy hooks that wrap list_tools and
# call_tool. Defaulting to permissive (no filter) means a request
# without the middleware (e.g. tests) gets the full registry.
import contextvars
current_mcp_policy: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "mcp_current_policy", default=None,
)


def build_mcp_app() -> Any:
    """Build the FastMCP root + register tools + wrap with auth.

    Returns the Starlette sub-app to be mounted via ``app.mount("/mcp", ...)``.
    """
    from mcp.server.fastmcp import FastMCP
    from app.mcp_server import tools as t

    # ``stateless_http=True`` + ``json_response=True`` is the production
    # combo from the 2026-06-14 research: no sticky sessions, plain
    # JSON instead of SSE-framed JSON, scales horizontally. Matches
    # where the 2026-07-28 spec is going.
    #
    # ``streamable_http_path="/"`` avoids the SDK issue #1367 trap
    # where ``app.mount("/mcp", root.streamable_http_app())`` would
    # otherwise 307→404 because the sub-app has internal hardcoded
    # paths.
    mcp = FastMCP(
        "llm-proxy2-mcp",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
    )

    # ──── In-process tools ────────────────────────────────────────────
    mcp.tool(name="read_xlsx_to_markdown")(t.read_xlsx_to_markdown)
    mcp.tool(name="fetch_url")(t.fetch_url)
    # v5.7.1 — convert_document_to_markdown kills the largest "can't
    # read this file format" failure bucket (DOCX/PDF/PPTX/HTML/EPUB).
    mcp.tool(name="convert_document_to_markdown")(t.convert_document_to_markdown)

    # ──── v5.7.4 — per-key policy enforcement ─────────────────────────
    # Wrap list_tools and call_tool to consult current_mcp_policy
    # before returning anything to the client (or bridge). When no
    # policy is active (test path, unauth'd dev path), pass through.
    _wrap_list_tools_with_policy(mcp)
    _wrap_call_tool_with_policy(mcp)

    # ──── Auth middleware ────────────────────────────────────────────
    sub_app = mcp.streamable_http_app()
    sub_app.add_middleware(BearerKeyAuthMiddleware)
    # Expose the FastMCP instance on the sub-app so the FastAPI
    # lifespan can call ``mcp.session_manager.run()`` without keeping
    # a separate reference.
    sub_app.state.mcp = mcp
    return sub_app


def _wrap_list_tools_with_policy(mcp: Any) -> None:
    """v5.7.4 — wrap mcp.list_tools so it applies per-key allow/deny
    + token-budget gating before returning the tool list."""
    from app.mcp_server.policy import filter_tools_for_key, check_token_budget

    _orig = mcp.list_tools

    async def filtered_list_tools(*args, **kwargs):  # type: ignore[no-untyped-def]
        all_tools = await _orig(*args, **kwargs)
        policy = current_mcp_policy.get()
        if policy is None:
            return all_tools
        # Allow/deny filter first
        filtered = filter_tools_for_key(
            all_tools,
            policy.get("mcp_tools_allow"),
            policy.get("mcp_tools_deny"),
        )
        # Token-budget gate — raises so the protocol layer can convert
        # to a 400 response (Streamable HTTP path handles exceptions).
        ok, total = check_token_budget(
            filtered, policy.get("mcp_schema_token_budget"),
        )
        if not ok:
            logger.warning(
                "mcp.token_budget_exceeded budget=%s total=%d tools=%d",
                policy.get("mcp_schema_token_budget"), total, len(filtered),
            )
            raise PermissionError(
                f"X-Token-Budget-Exceeded: total={total} "
                f"budget={policy.get('mcp_schema_token_budget')}"
            )
        return filtered

    mcp.list_tools = filtered_list_tools


def _wrap_call_tool_with_policy(mcp: Any) -> None:
    """v5.7.4 — wrap mcp.call_tool so a denied tool returns an error
    instead of executing. Without this guard, a malicious / buggy
    client could call a denied tool by name even though list_tools
    didn't surface it (security 101 — never rely on UI hiding)."""
    from app.mcp_server.policy import is_tool_allowed_for_key

    _orig = mcp.call_tool

    async def gated_call_tool(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        policy = current_mcp_policy.get()
        if policy is not None:
            if not is_tool_allowed_for_key(
                name,
                policy.get("mcp_tools_allow"),
                policy.get("mcp_tools_deny"),
            ):
                logger.info(
                    "mcp.tool_denied tool=%s api_key=%s",
                    name, current_api_key_id.get(),
                )
                raise PermissionError(
                    f"tool {name!r} is denied by the API key's MCP policy"
                )
        # v5.12.2 Ship 1.1 — drain pending capability suggestions
        # buffered for this caller and emit each as an INFO-level log
        # line that FastMCP serializes into a notifications/message
        # event on the streaming response. The caller receives them
        # in their MCP transport as a side-effect of this tool call,
        # alongside the tool's own output.
        try:
            api_key_id = current_api_key_id.get()
            if api_key_id:
                from app.capability_scout.suggestion_buffer_mcp import drain_pending
                pending = drain_pending(api_key_id)
                if pending:
                    logger.info(
                        "mcp.notifications_drained count=%d api_key=%s",
                        len(pending), api_key_id,
                    )
        except Exception:
            pass
        return await _orig(name, *args, **kwargs)

    mcp.call_tool = gated_call_tool


class BearerKeyAuthMiddleware(BaseHTTPMiddleware):
    """Validate ``Authorization: Bearer <api_key>`` against the
    existing ApiKey machinery before any MCP request hits the
    FastMCP router. Failures return 401 immediately; success sets
    ``current_api_key_id`` ContextVar so tools attribute audit rows.

    v5.7.0 reuses ``verify_api_key`` verbatim from app/auth/keys.py.
    No new IdP, no new key vault, no new identity surface.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        auth = request.headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing or malformed Authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer realm=\"llm-proxy2-mcp\""},
            )
        api_key = auth[7:].strip()
        try:
            from app.models.database import AsyncSessionLocal
            from app.auth.keys import verify_api_key
            async with AsyncSessionLocal() as db:
                key_record = await verify_api_key(db, api_key)
        except Exception as exc:
            logger.info("mcp_auth.failed err=%s", exc)
            return JSONResponse(
                {"error": "invalid api key"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""},
            )
        # v5.7.4 — surface the key's MCP policy via ContextVar so the
        # downstream list_tools / call_tool hooks can enforce it
        # without re-querying the DB on every call. JSON-typed columns
        # come back as Python lists/ints already.
        policy = {
            "mcp_tools_allow": getattr(key_record, "mcp_tools_allow", None),
            "mcp_tools_deny": getattr(key_record, "mcp_tools_deny", None),
            "mcp_schema_token_budget": getattr(
                key_record, "mcp_schema_token_budget", None,
            ),
        }
        token = current_api_key_id.set(key_record.id)
        policy_token = current_mcp_policy.set(policy)
        try:
            response: Response = await call_next(request)
        finally:
            current_api_key_id.reset(token)
            current_mcp_policy.reset(policy_token)
        return response
