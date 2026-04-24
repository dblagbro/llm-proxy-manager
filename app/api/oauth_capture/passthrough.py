"""The actual forwarding/recording endpoint for the OAuth capture package.

Every request that lands at ``/api/oauth-capture/{profile_name}/{path:path}``
arrives here. We load the profile, verify the secret, forward to the
profile's upstream, record both halves of the exchange, and return the
upstream response unchanged.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from app.models.database import AsyncSessionLocal
from app.models.db import OAuthCaptureLog, OAuthCaptureProfile

from app.api.oauth_capture.serializers import (
    _filter_req_headers, _filter_resp_headers, _safe_text,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/oauth-capture", tags=["oauth-capture"])


@router.api_route(
    "/{profile_name}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def capture_passthrough(profile_name: str, path: str, request: Request):
    """Forward the request to the profile's upstream and log everything."""
    async with AsyncSessionLocal() as db:
        profile = (await db.execute(
            select(OAuthCaptureProfile).where(OAuthCaptureProfile.name == profile_name)
        )).scalar_one_or_none()

    if profile is None:
        raise HTTPException(404, f"Capture profile {profile_name!r} not found")
    if not profile.enabled:
        raise HTTPException(403, f"Capture profile {profile_name!r} is disabled")
    if not profile.upstream_urls:
        raise HTTPException(500, "Profile has no upstream URLs configured")

    # Secret check — v2.6.2:
    # Trust direct docker-internal requests (sidecar → llm-proxy2 on the
    # internal network, no nginx in the loop, so no X-Forwarded-For).
    # Public workstation traffic flows through nginx which always sets
    # X-Forwarded-For, so those requests still need ?cap=SECRET or the
    # X-Capture-Secret header.
    internal = not request.headers.get("x-forwarded-for")
    if profile.secret and not internal:
        provided = request.query_params.get("cap") or request.headers.get("x-capture-secret")
        if provided != profile.secret:
            raise HTTPException(403, "Capture secret required")
        query = {k: v for k, v in request.query_params.multi_items() if k != "cap"}
        query_string = "&".join(f"{k}={v}" for k, v in query)
    else:
        # Strip cap= if it snuck in anyway (e.g. legacy sidecar builds).
        query = {k: v for k, v in request.query_params.multi_items() if k != "cap"}
        query_string = "&".join(f"{k}={v}" for k, v in query)

    session_tag = request.headers.get("x-capture-session") or None

    # Pick upstream: first URL wins for now. Future: path-prefix routing so
    # OAuth URLs go to auth host and API calls go to api host.
    upstream = profile.upstream_urls[0]
    target = f"{upstream}/{path}"
    if query_string:
        target = f"{target}?{query_string}"

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
                profile_name=profile_name,
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
