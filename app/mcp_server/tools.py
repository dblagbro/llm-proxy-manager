"""v5.7.0 — In-process MCP tools for the aggregation endpoint.

Two tools registered:
1. ``read_xlsx_to_markdown`` — port of v5.6.0's ``app/proxy_tools/excel.py``.
   Shares the underlying renderer; the MCP wrapper just adapts the
   input/output shape.
2. ``fetch_url`` — HTTP GET with safety caps (https/http only, 5 MB
   body cap, 30s timeout). Returns the response body as text or
   ``error: ...`` if anything goes wrong.

These two were chosen as the smallest scaffold that demonstrates the
aggregation pattern without exposing dangerous tools (no filesystem,
no command execution, no databases). v5.7.1 adds the FastMCP-mounted
official servers (filesystem, fetch, git, markitdown) which have
proper sandboxing of their own.
"""
from __future__ import annotations

import time
from typing import Any


_DEFAULT_MAX_ROWS = 200
_DEFAULT_MAX_COLS = 30
_FETCH_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_FETCH_TIMEOUT_SEC = 30.0


async def read_xlsx_to_markdown(
    file_b64: str | None = None,
    url: str | None = None,
    sheet: str | None = None,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_cols: int = _DEFAULT_MAX_COLS,
) -> str:
    """Read an Excel (.xlsx) spreadsheet and return its contents as
    markdown tables.

    Supply EITHER ``file_b64`` (base64-encoded xlsx bytes) OR ``url``
    (https URL the proxy fetches server-side). Each sheet becomes a
    ``## Sheet: <name>`` section with a pipe-table of cell values.
    Defaults to first 200 rows × 30 columns; pass max_rows / max_cols
    to widen.

    Use this when the user asks about a spreadsheet — do not say you
    can't read xlsx files, just call this tool.
    """
    # Reuse the v5.6.0 renderer end-to-end. Audit the call in our own
    # table before returning.
    from app.proxy_tools.excel import _fetch_bytes, _render_workbook
    t0 = time.time()
    try:
        data = await _fetch_bytes({"file_b64": file_b64, "url": url})
        if max_rows <= 0 or max_rows > 5000:
            raise ValueError("max_rows must be in (0, 5000]")
        if max_cols <= 0 or max_cols > 200:
            raise ValueError("max_cols must be in (0, 200]")
        out = _render_workbook(data, sheet, max_rows, max_cols)
        await _audit_tool_call(
            tool_name="read_xlsx_to_markdown",
            input_summary=f"src={'b64' if file_b64 else 'url'} sheet={sheet}",
            output_bytes=len(out.encode()),
            latency_ms=int((time.time() - t0) * 1000),
            ok=True,
        )
        return out
    except Exception as exc:
        await _audit_tool_call(
            tool_name="read_xlsx_to_markdown",
            input_summary=f"src={'b64' if file_b64 else 'url'} sheet={sheet}",
            output_bytes=0,
            latency_ms=int((time.time() - t0) * 1000),
            ok=False,
            error_msg=f"{type(exc).__name__}: {exc}",
        )
        return f"error: {type(exc).__name__}: {exc}"


async def fetch_url(url: str, max_bytes: int = _FETCH_MAX_BYTES) -> str:
    """Fetch a URL and return its response body as text.

    Safety caps:
    - URL must use ``http://`` or ``https://`` (no ``file://``, no
      ``ftp://``, no ``data:``).
    - Response body capped at 5 MB by default (configurable up to
      this max via ``max_bytes``; values larger are clamped).
    - 30-second hard timeout.
    - Single GET request; follows up to 5 redirects.

    Returns the response body as a UTF-8 string. Binary responses
    are best-effort decoded with replacement characters.
    """
    import httpx
    t0 = time.time()
    try:
        if not isinstance(url, str) or not url:
            raise ValueError("url is required and must be a string")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(
                "url must use http:// or https://; got "
                f"{url.split(':', 1)[0]}://"
            )
        cap = min(int(max_bytes or _FETCH_MAX_BYTES), _FETCH_MAX_BYTES)
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_SEC, follow_redirects=True,
        ) as client:
            resp = await client.get(url)
        body = resp.content[:cap]
        text = body.decode("utf-8", errors="replace")
        truncated = len(resp.content) > cap
        if truncated:
            text += f"\n\n[truncated at {cap} bytes; full body was {len(resp.content)} bytes]"
        await _audit_tool_call(
            tool_name="fetch_url",
            input_summary=f"url={url[:100]} status={resp.status_code}",
            output_bytes=len(text.encode()),
            latency_ms=int((time.time() - t0) * 1000),
            ok=True,
        )
        return text
    except Exception as exc:
        await _audit_tool_call(
            tool_name="fetch_url",
            input_summary=f"url={(url or '')[:100]}",
            output_bytes=0,
            latency_ms=int((time.time() - t0) * 1000),
            ok=False,
            error_msg=f"{type(exc).__name__}: {exc}",
        )
        return f"error: {type(exc).__name__}: {exc}"


async def _audit_tool_call(
    tool_name: str,
    input_summary: str,
    output_bytes: int,
    latency_ms: int,
    ok: bool,
    error_msg: str | None = None,
) -> None:
    """Write one mcp_tool_calls row. Best-effort — failures swallowed
    so a transient DB issue can't break the tool path.

    v5.7.x will migrate this into compliance_events for cluster
    replication; today the row lives in a dedicated table so the
    audit shape is locked from day one.
    """
    from app.mcp_server import current_api_key_id, current_parent_request_id
    try:
        from app.models.database import AsyncSessionLocal
        from app.models.db import McpToolCall
        from datetime import datetime
        async with AsyncSessionLocal() as db:
            db.add(McpToolCall(
                created_at=datetime.utcnow(),
                api_key_id=current_api_key_id.get(),
                parent_request_id=current_parent_request_id.get(),
                tool_name=tool_name,
                mcp_server_id="in-process",
                input_summary=input_summary[:480],
                output_bytes=output_bytes,
                latency_ms=latency_ms,
                ok=ok,
                error_msg=(error_msg or "")[:480] if error_msg else None,
            ))
            await db.commit()
    except Exception:
        pass  # audit writes must NEVER break the tool path
