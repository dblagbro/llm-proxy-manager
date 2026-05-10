"""v3.7.2 — narrower admin scope for the model-identity catalog API.

Committed to the coordinator-hub team in the #230 contract reply on
2026-05-09: their hub-side `proxy_admin_key_enc` should be swappable
in-place for a narrower-scoped key that can only edit the catalog,
without giving the hub access to the full admin surface (provider
secrets, billing scrapes, user management, etc.).

This module adds two things:

1. New ``key_type`` values for ``api_keys`` rows:
   - ``admin`` — full admin via Bearer/x-api-key (parity with session admin)
   - ``admin-readonly-catalog`` — scoped to the model-identity catalog
     endpoints only. Can do GET + PUT on ``/api/llm/models/*`` and
     nothing else.

2. A new FastAPI dependency ``require_catalog_auth`` that accepts:
   - the existing admin session (cookie or Bearer-session-token), OR
   - a Bearer / x-api-key API key whose row has
     ``key_type IN ('admin', 'admin-readonly-catalog')``.

Wire this dep onto the ``/api/llm/models/{model_id}`` GET/PUT routes
in place of ``require_admin``. Everything else keeps using
``require_admin`` so we don't accidentally widen the surface of any
other endpoint.

When the hub team rotates from their session-Bearer admin key to a
real admin-readonly-catalog API key, behavior is byte-identical from
their perspective — same Authorization header shape, same response
contract. Pure auth-back-end swap.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, _extract_token, _get_session
from app.models.database import get_db

logger = logging.getLogger(__name__)

# v3.7.2 — accepted key_type values for catalog-scope auth. Bearer /
# x-api-key with one of these grants access to the model-identity
# catalog endpoints. ``admin-readonly-catalog`` is the narrow scope
# the hub team requested; ``admin`` is a wider hatch we ship at the
# same time so operators can create one "everything" admin key
# without having to also keep a session alive.
_CATALOG_ALLOWED_KEY_TYPES = frozenset({"admin", "admin-readonly-catalog"})


async def _try_session_admin(request: Request) -> Optional[AdminUser]:
    """Try the existing session-cookie/Bearer-session admin flow.
    Returns ``None`` if no session token was supplied OR the session
    isn't an admin one — caller falls through to the api-key path.
    """
    token = _extract_token(request)
    if not token:
        return None
    s = await _get_session(token)
    if not s:
        return None
    if s.get("role") != "admin":
        return None
    return AdminUser(user_id=s["user_id"], username=s["username"], role=s["role"])


async def _try_catalog_apikey(
    request: Request, db: AsyncSession,
) -> Optional[AdminUser]:
    """Try the new admin-scoped api-key flow.

    Looks for an api_key value in either the ``x-api-key`` header or
    a Bearer-shaped Authorization header (after the session-token
    path was tried first and didn't match — there's no ambiguity
    because session tokens are URL-safe random strings while api
    keys are prefixed ``llmp-``).

    Returns ``None`` when no key is supplied OR the key's row doesn't
    have a catalog-allowed ``key_type``. Caller raises 401/403 in
    that case.
    """
    raw_key: Optional[str] = request.headers.get("x-api-key")
    if not raw_key:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:].startswith("llmp-"):
            raw_key = auth[7:]
    if not raw_key:
        return None
    # Look up the api key by its sha256 hash (same as verify_api_key)
    import hashlib
    from sqlalchemy import select
    from app.models.db import ApiKey
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    rs = await db.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash).where(ApiKey.enabled == True)
    )
    key = rs.scalar_one_or_none()
    if key is None:
        return None
    if (key.key_type or "") not in _CATALOG_ALLOWED_KEY_TYPES:
        # Wrong scope — better to return None and let the caller emit
        # a clear 403, so distinct from "no auth at all" (401).
        return None
    # Synthesize an AdminUser so downstream code that expects one
    # works uniformly. ``role`` reflects the scope so logs show the
    # narrower variant.
    return AdminUser(
        user_id=f"apikey:{key.id}",
        username=f"apikey:{key.name}",
        role=key.key_type or "admin-readonly-catalog",
    )


async def require_catalog_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AdminUser:
    """FastAPI dependency for the model-identity catalog endpoints.

    Accepts EITHER:
      - admin session (cookie or Bearer session-token), OR
      - x-api-key / Bearer api-key whose row has
        ``key_type IN ('admin', 'admin-readonly-catalog')``.

    Order: try session first (the existing flow + cheaper), then api-key.

    Raises 401 if neither is present. Raises 403 if api-key path
    found a key but with the wrong scope.
    """
    # Path 1: existing session admin
    admin = await _try_session_admin(request)
    if admin is not None:
        return admin
    # Path 2: api-key with catalog scope
    admin = await _try_catalog_apikey(request, db)
    if admin is not None:
        return admin
    # No valid auth found. Distinguish 401 (nothing supplied) from 403
    # (something supplied but wrong scope) for clearer hub-side debug.
    raw_key = request.headers.get("x-api-key")
    if not raw_key:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:].startswith("llmp-"):
            raw_key = auth[7:]
    if raw_key:
        # We saw an api key but it didn't have catalog scope
        raise HTTPException(
            403,
            "API key lacks model-catalog scope. Required key_type: "
            "'admin' or 'admin-readonly-catalog'.",
        )
    # No auth material at all
    raise HTTPException(401, "Not authenticated")
