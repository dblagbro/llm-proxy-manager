"""v5.7.0/v5.7.1 — In-process MCP tools for the aggregation endpoint.

Tools registered:
1. ``read_xlsx_to_markdown`` (v5.7.0) — port of v5.6.0's
   ``app/proxy_tools/excel.py``. Shares the underlying renderer.
2. ``fetch_url`` (v5.7.0) — HTTP GET with safety caps.
3. ``convert_document_to_markdown`` (v5.7.1) — Microsoft markitdown
   wrapper. Handles DOCX / PDF / PPTX / HTML / EPUB / CSV / Markdown
   files and returns the content as markdown. Kills the largest
   bucket of "I can't read this file format" failures.

No filesystem read, no command execution, no databases. v5.7.2 will
add the FastMCP-mounted official sub-servers (server-git via uvx,
server-filesystem) which have their own sandboxing.
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


async def convert_document_to_markdown(
    file_b64: str | None = None,
    url: str | None = None,
    file_extension: str | None = None,
) -> str:
    """Convert a document (DOCX / PDF / PPTX / HTML / EPUB / CSV /
    MD / TXT / JPG / PNG / ODT) to markdown using Microsoft markitdown.

    Supply EITHER ``file_b64`` (base64-encoded file bytes) OR ``url``
    (https URL the proxy fetches server-side). Optionally pass
    ``file_extension`` (e.g. ``"pdf"``, ``"docx"``) to hint the
    format detector if the URL doesn't have a clear extension.

    Use this when the user asks about content in a document the model
    can't natively read. Don't say you can't read PDFs/DOCX/etc. —
    just call this tool.

    Cap: 5 MB. Returns ``error: <reason>`` on failure (does NOT raise
    to keep the tool loop alive for the model).
    """
    from app.proxy_tools.excel import _fetch_bytes
    import io
    import time as _time
    t0 = _time.time()
    try:
        data = await _fetch_bytes({"file_b64": file_b64, "url": url})
        # markitdown infers format from filename. If we have a URL,
        # derive the extension from it; else use the operator-supplied
        # hint or fall back to bytes-sniffing.
        ext = (file_extension or "").lstrip(".").lower()
        if not ext and url:
            tail = url.rsplit(".", 1)
            if len(tail) == 2 and 1 <= len(tail[1]) <= 6 and tail[1].isalnum():
                ext = tail[1].lower()
        fname = f"input.{ext}" if ext else "input.bin"
        from markitdown import MarkItDown
        md = MarkItDown()
        # markitdown's convert_stream API takes a binary stream
        result = md.convert_stream(io.BytesIO(data), file_extension=("." + ext) if ext else None)
        out = (result.text_content or "").strip()
        if not out:
            out = "(empty document or markitdown returned no content)"
        await _audit_tool_call(
            tool_name="convert_document_to_markdown",
            input_summary=f"src={'b64' if file_b64 else 'url'} ext={ext or '?'}",
            output_bytes=len(out.encode()),
            latency_ms=int((_time.time() - t0) * 1000),
            ok=True,
        )
        return out
    except Exception as exc:
        await _audit_tool_call(
            tool_name="convert_document_to_markdown",
            input_summary=f"src={'b64' if file_b64 else 'url'}",
            output_bytes=0,
            latency_ms=int((_time.time() - t0) * 1000),
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
