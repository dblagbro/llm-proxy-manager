"""OAuth passthrough capture — research tool for the Claude Pro Max OAuth
provider backlog item.

How to use:
  1. Set the upstream in settings.oauth_capture_upstream
     (e.g. "https://console.anthropic.com"). No trailing slash.
  2. Point the claude-code CLI at our proxy:
       ANTHROPIC_BASE_URL=https://your-proxy/llm-proxy2/api/oauth-capture
       ANTHROPIC_AUTH_URL=https://your-proxy/llm-proxy2/api/oauth-capture
     (The exact env-var names may differ — anything that makes the CLI
     hit our path instead of the real host will work.)
  3. Run `claude` / `claude login` on the workstation.
  4. Every request that lands at /api/oauth-capture/<path> is:
       - logged (headers + body + query + response)
       - forwarded to {oauth_capture_upstream}/<path>
       - the upstream response is returned to the client unchanged.
  5. Query captures:
       GET /api/oauth-capture/_log       list recent captures (admin)
       GET /api/oauth-capture/_log/{id}  full record including bodies
       GET /api/oauth-capture/_export    NDJSON dump for reverse-eng

Security:
  - The endpoint is *not* authenticated — authentication would break the
    CLI's OAuth flow. To protect it, gate the feature behind the
    oauth_capture_enabled setting (default off) and a long, hard-to-guess
    path prefix bearer via settings.oauth_capture_secret (optional). When
    oauth_capture_secret is set, the caller must include it as the query
    parameter ?cap=<secret> or the request is 403'd before being
    forwarded.
  - Captured bodies may contain client secrets / authorization codes /
    bearer tokens. Treat oauth_capture_log like any other secret store.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import require_admin, AdminUser
from app.config import settings
from app.models.database import AsyncSessionLocal, get_db
from app.models.db import OAuthCaptureLog

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/oauth-capture", tags=["oauth-capture"])


# ── Header filtering ─────────────────────────────────────────────────────────
# Hop-by-hop headers that must not be copied to the upstream.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "content-length",   # httpx will set its own
    "host",             # we're sending to a different host
})


def _filter_req_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _filter_resp_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


# ── Admin endpoints (must be before the catch-all) ──────────────────────────


@router.get("/_log")
async def list_captures(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(
        select(OAuthCaptureLog)
        .order_by(OAuthCaptureLog.id.desc())
        .limit(min(limit, 500))
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "method": r.method,
            "path": r.path,
            "upstream_url": r.upstream_url,
            "resp_status": r.resp_status,
            "latency_ms": r.latency_ms,
            "error": r.error,
            "req_body_preview": (r.req_body or "")[:200],
            "resp_body_preview": (r.resp_body or "")[:200],
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/_log/{log_id}")
async def get_capture(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    result = await db.execute(select(OAuthCaptureLog).where(OAuthCaptureLog.id == log_id))
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Capture not found")
    return {
        "id": r.id,
        "capture_session": r.capture_session,
        "method": r.method,
        "path": r.path,
        "upstream_url": r.upstream_url,
        "req_headers": r.req_headers,
        "req_body": r.req_body,
        "req_query": r.req_query,
        "resp_status": r.resp_status,
        "resp_headers": r.resp_headers,
        "resp_body": r.resp_body,
        "latency_ms": r.latency_ms,
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/_export")
async def export_captures(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """NDJSON dump of all captures for offline analysis."""
    result = await db.execute(select(OAuthCaptureLog).order_by(OAuthCaptureLog.id))
    rows = result.scalars().all()

    async def _gen():
        for r in rows:
            rec = {
                "id": r.id,
                "method": r.method,
                "path": r.path,
                "upstream_url": r.upstream_url,
                "req_headers": r.req_headers,
                "req_body": r.req_body,
                "req_query": r.req_query,
                "resp_status": r.resp_status,
                "resp_headers": r.resp_headers,
                "resp_body": r.resp_body,
                "latency_ms": r.latency_ms,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            yield (json.dumps(rec) + "\n").encode()

    return StreamingResponse(_gen(), media_type="application/x-ndjson")


@router.delete("/_log")
async def clear_captures(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Wipe the capture log. Use before starting a new OAuth flow recording."""
    from sqlalchemy import delete as sql_delete
    await db.execute(sql_delete(OAuthCaptureLog))
    await db.commit()
    return {"ok": True}


# ── Passthrough catch-all ────────────────────────────────────────────────────


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def capture_passthrough(path: str, request: Request):
    """Forward the request to the configured upstream and record everything.

    Auth model (intentionally soft): this endpoint proxies unauthenticated
    traffic to an external service, so a stolen URL could be abused as an
    open relay. The upstream-URL whitelist + oauth_capture_secret check
    below limits the blast radius to the single host we want to capture
    from (console.anthropic.com by default).
    """
    if not getattr(settings, "oauth_capture_enabled", False):
        raise HTTPException(404, "OAuth capture not enabled")

    upstream_host = getattr(settings, "oauth_capture_upstream", None)
    if not upstream_host:
        raise HTTPException(500, "oauth_capture_upstream not configured")
    upstream_host = upstream_host.rstrip("/")

    # Optional per-request secret to prevent drive-by abuse
    secret = getattr(settings, "oauth_capture_secret", None)
    if secret:
        provided = request.query_params.get("cap") or request.headers.get("x-capture-secret")
        if provided != secret:
            raise HTTPException(403, "Capture secret required")
        # Strip our secret param so it's not forwarded upstream
        q = {k: v for k, v in request.query_params.multi_items() if k != "cap"}
        query_string = "&".join(f"{k}={v}" for k, v in q)
    else:
        query_string = request.url.query

    session_tag = request.headers.get("x-capture-session") or None

    # Build target URL
    target = f"{upstream_host}/{path}"
    if query_string:
        target = f"{target}?{query_string}"

    # Read body and headers
    body_bytes = await request.body()
    req_headers_dict = dict(request.headers.items())
    forward_headers = _filter_req_headers(req_headers_dict)

    t0 = time.monotonic()
    resp_status: Optional[int] = None
    resp_headers_dict: dict = {}
    resp_body_bytes: bytes = b""
    error_msg: Optional[str] = None

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            r = await client.request(
                request.method,
                target,
                content=body_bytes if body_bytes else None,
                headers=forward_headers,
            )
            resp_status = r.status_code
            resp_headers_dict = dict(r.headers.items())
            resp_body_bytes = r.content
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.warning("oauth_capture.upstream_failed %s", error_msg)

    latency_ms = (time.monotonic() - t0) * 1000.0

    # Persist the capture
    try:
        async with AsyncSessionLocal() as db:
            log = OAuthCaptureLog(
                capture_session=session_tag,
                method=request.method,
                path=path,
                upstream_url=target,
                req_headers=req_headers_dict,
                req_body=_safe_text(body_bytes),
                req_query=query_string,
                resp_status=resp_status,
                resp_headers=resp_headers_dict,
                resp_body=_safe_text(resp_body_bytes),
                latency_ms=latency_ms,
                error=error_msg,
            )
            db.add(log)
            await db.commit()
    except Exception as exc:
        logger.error("oauth_capture.log_insert_failed %s", exc)

    # Surface the upstream response to the caller
    if error_msg is not None:
        return JSONResponse(
            {"error": "upstream_unreachable", "detail": error_msg},
            status_code=502,
        )

    return Response(
        content=resp_body_bytes,
        status_code=resp_status or 502,
        headers=_filter_resp_headers(resp_headers_dict),
        media_type=resp_headers_dict.get("content-type"),
    )


def _safe_text(raw: bytes) -> Optional[str]:
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        import base64
        return "[binary:" + base64.b64encode(raw[:4096]).decode() + "]"
