"""v5.0.0 — Allowed-paths enforcement middleware (spec §8.3, decision 21).

Locks an API key to an explicit list of normalized request paths. EXACT match
only — no substring / prefix collisions. Globs deferred to v5.1.

Lookup model mirrors ``ip_block.py``: cheap path-prefix bail-outs, then a
single DB read to resolve the API key. On miss we write a
``compliance_events`` row (``path_not_allowed`` / ``path-not-in-allowed_paths``,
HTTP 403) so the operator has an audit trail of every refusal.

Debug-echo bypass: when ``key.debug_echo_enabled=True``, requests to
``/api/debug/echo-client`` bypass the check (sandbox-only escape hatch —
production keys keep ``debug_echo_enabled=False``).

Defensive: on any unexpected exception we fail OPEN. Refusing every
request because of a DB hiccup would be worse than the misconfig leak.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from app.compliance import (
    emit_event,
    generate_audit_id,
    refusal_headers_path,
)

logger = logging.getLogger(__name__)

# Paths that should never gate on allowed_paths. The auth endpoints + health
# are public; the cluster sync surface is HMAC-authenticated; metrics are
# unauthenticated by design. Avoids bricking a key that fat-fingers its
# allowed_paths list.
_BYPASS_PREFIXES = (
    "/health", "/version", "/metrics", "/favicon",
    "/api/auth/", "/cluster/", "/assets/",
)


def _normalize_path(path: str) -> str:
    """Strip the nginx ``/llm-proxy2`` prefix + trailing slash + collapse
    double-slashes so the stored allowed_paths list compares cleanly
    regardless of deployment prefix."""
    if not path:
        return "/"
    p = path
    # Match the longer "-smoke" suffix first so the bare prefix doesn't eat
    # part of it.
    for prefix in ("/llm-proxy2-smoke", "/llm-proxy2"):
        if p.startswith(prefix):
            p = p[len(prefix):] or "/"
            break
    # Collapse double slashes
    while "//" in p:
        p = p.replace("//", "/")
    # Strip trailing slash (except root)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def _is_bypass_path(path: str) -> bool:
    return any(path.startswith(p) for p in _BYPASS_PREFIXES)


def _extract_raw_key(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.headers.get("x-api-key")


async def _resolve_api_key(raw_key: str):
    """Return the ApiKey ORM row for ``raw_key``, or None."""
    import hashlib
    from sqlalchemy import select
    from app.models.database import AsyncSessionLocal
    from app.models.db import ApiKey
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with AsyncSessionLocal() as db:
        rs = await db.execute(
            select(ApiKey).where(
                ApiKey.key_hash == key_hash,
                ApiKey.enabled == True,  # noqa: E712
                ApiKey.deleted_at.is_(None),
            )
        )
        return rs.scalar_one_or_none()


async def _emit_path_block_event(api_key_id: str, path: str, ua: Optional[str]) -> str:
    """Write the ``path_not_allowed`` compliance row in its own session
    (commit=True). Returns the generated audit_id so callers can echo
    it in the 403 response + X-Compliance-Audit-Id header."""
    audit_id = generate_audit_id()
    try:
        from app.models.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            await emit_event(
                db,
                audit_id=audit_id,
                api_key_id=api_key_id,
                event_type="path_not_allowed",
                reason_code="path-not-in-allowed_paths",
                http_status=403,
                client_user_agent=ua,
                commit=True,
            )
    except Exception as exc:
        # Audit row failure must not block the 403 response itself.
        logger.warning("allowed_paths.audit_write_failed err=%s", exc)
    return audit_id


async def allowed_paths_middleware(request: Request, call_next):
    """Enforce ``ApiKey.allowed_paths`` exact-match on every request that
    presents an API key. No key → pass through (auth layer handles 401).
    No allowed_paths configured → pass through (unrestricted, legacy
    behavior)."""
    try:
        path = _normalize_path(request.url.path)
        if _is_bypass_path(path):
            return await call_next(request)
        raw_key = _extract_raw_key(request)
        if not raw_key:
            # Auth layer will 401 the request downstream.
            return await call_next(request)
        key = await _resolve_api_key(raw_key)
        if key is None or key.allowed_paths is None:
            return await call_next(request)
        # Debug-echo bypass: sandbox-only escape hatch (decision X).
        if getattr(key, "debug_echo_enabled", False) and path == "/api/debug/echo-client":
            return await call_next(request)
        allowed = set(key.allowed_paths or [])
        if path in allowed:
            return await call_next(request)
        ua = request.headers.get("user-agent")
        audit_id = await _emit_path_block_event(key.id, path, ua)
        return JSONResponse(
            {
                "error": "path_not_allowed",
                "requested_path": path,
                "allowed_paths": list(key.allowed_paths or []),
                "audit_id": audit_id,
            },
            status_code=403,
            headers=refusal_headers_path(audit_id=audit_id),
        )
    except Exception as exc:
        logger.warning("allowed_paths.check_failed err=%s", exc)
        return await call_next(request)
