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

    # ──── Auth middleware ────────────────────────────────────────────
    sub_app = mcp.streamable_http_app()
    sub_app.add_middleware(BearerKeyAuthMiddleware)
    # Expose the FastMCP instance on the sub-app so the FastAPI
    # lifespan can call ``mcp.session_manager.run()`` without keeping
    # a separate reference.
    sub_app.state.mcp = mcp
    return sub_app


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
        token = current_api_key_id.set(key_record.id)
        try:
            response: Response = await call_next(request)
        finally:
            current_api_key_id.reset(token)
        return response
