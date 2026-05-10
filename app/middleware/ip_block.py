"""v3.7.11 — IP block middleware.

Returns 403 early for any request whose source IP matches the
``blocked_ips`` table. Checks BOTH the raw inside IP and the
LAN-egress rewritten public IP (per v3.6.3's hairpin-NAT logic), so
operators can block either the actual source or the LAN's outside
NAT depending on their attribution model.

In-memory cache with 30s TTL — the middleware runs on every request
hot path, so we avoid the DB hit. New blocks added via admin
endpoint propagate within ~30s (or instantly on the node that did
the write; peer nodes pick it up on cluster sync + next refresh).

The middleware is registered ahead of the auth/inference path so
blocked traffic is rejected before any expensive work runs. Health
checks and the admin-login endpoint are intentionally NOT exempted
— a blocked IP gets a uniform 403 across the whole surface so
attackers can't probe for which endpoints are open.

Defensive: if the cache load fails (DB timeout, etc.) we fail OPEN
— better to let traffic through than to 403 every request because
of an unrelated DB hiccup.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# In-process cache. ``_loaded_at`` = monotonic seconds. ``_blocked``
# is a frozenset for O(1) membership tests.
_blocked: frozenset[str] = frozenset()
_loaded_at: float = 0.0
_TTL_SEC = 30.0


async def _load_blocked_set() -> frozenset[str]:
    """Read the full ``blocked_ips`` table into an in-memory set.
    Called every TTL window. Defensive — returns empty set on error
    so we fail-open rather than 403-everything."""
    try:
        from sqlalchemy import select
        from app.models.database import AsyncSessionLocal
        from app.models.db import BlockedIp
        async with AsyncSessionLocal() as db:
            rs = await db.execute(select(BlockedIp.ip))
            return frozenset(r[0] for r in rs.all() if r[0])
    except Exception as exc:
        logger.warning("ip_block.cache_load_failed err=%s", exc)
        return frozenset()


async def _get_blocked_set() -> frozenset[str]:
    global _blocked, _loaded_at
    now = time.monotonic()
    if now - _loaded_at > _TTL_SEC:
        _blocked = await _load_blocked_set()
        _loaded_at = now
    return _blocked


def _clear_cache_for_tests() -> None:
    """Test helper to force a reload on next request."""
    global _blocked, _loaded_at
    _blocked = frozenset()
    _loaded_at = 0.0


async def is_blocked(ip: Optional[str]) -> bool:
    if not ip:
        return False
    return ip in (await _get_blocked_set())


async def ip_block_middleware(request: Request, call_next):
    """FastAPI HTTP middleware. Check raw + rewritten IP; reject if
    either is on the block list.

    Order: this should be registered BEFORE log_requests so it runs
    AFTER log_requests sets the request-context client_ip. (FastAPI
    middleware registered later wraps registered-earlier — runs
    outermost first. To run inside log_requests, register first.)
    """
    try:
        from app.observability.request_context import (
            extract_client_ip_from_request,
            _maybe_rewrite_lan_ip,
        )
        raw_ip = extract_client_ip_from_request(request)
        rewritten = _maybe_rewrite_lan_ip(raw_ip) if raw_ip else None
        # Check both forms — operator may block either
        if await is_blocked(raw_ip) or (rewritten and await is_blocked(rewritten)):
            logger.info(
                "ip_block.rejected raw=%s rewritten=%s path=%s",
                raw_ip, rewritten, request.url.path,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Source IP is blocked by administrator."},
            )
    except Exception as exc:
        # Fail open — don't 500 the request because of a check error
        logger.warning("ip_block.check_failed err=%s", exc)
    return await call_next(request)
