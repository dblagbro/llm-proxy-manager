"""Cluster coordination endpoints."""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.auth.admin import require_admin, AdminUser
from app.cluster.manager import get_cluster_status, apply_sync, peers as cluster_peers
from app.cluster.auth import verify_cluster_request, sign_payload, verify_payload
from app.routing.circuit_breaker import get_all_states
from app.config import settings
from app import config_runtime
from app.__version__ import __version__

logger = logging.getLogger(__name__)
router = APIRouter(tags=["cluster"])


# v3.0.24 (#136): /health is hit by docker healthcheck every 30s + cluster
# peers' heartbeat every 30s. On a 3-node cluster with both sources active,
# that's ~270 hits/hour per node — every one previously hit the DB (SELECT
# providers + per-provider is_available) just to compute the same answer.
# Cache the response for 3s; well under the 30s heartbeat cadence so peer
# state is still fresh, and CB state still reads from in-memory.
import time as _time
_HEALTH_CACHE: dict = {"ts": 0.0, "body": None}
_HEALTH_CACHE_TTL_SEC = 3.0


@router.get("/health")
async def health():
    """Public health endpoint — also used by cluster peers for heartbeat.
    DB lookup result is cached for 3 seconds; CB state is always live.
    """
    now = _time.time()
    if _HEALTH_CACHE["body"] is not None and now - _HEALTH_CACHE["ts"] < _HEALTH_CACHE_TTL_SEC:
        # Re-evaluate CB state on every call; only the provider count is cached.
        cached = _HEALTH_CACHE["body"]
        return {
            **cached,
            "circuitBreakers": get_all_states(),
        }

    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider
    from sqlalchemy import select
    from app.routing.circuit_breaker import is_available

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Provider).where(Provider.enabled == True))
        providers = result.scalars().all()

    total = len(providers)
    healthy = 0
    for p in providers:
        if await is_available(p.id):
            healthy += 1

    body = {
        "status": "healthy" if healthy > 0 else "degraded",
        "version": __version__,
        "nodeId": settings.cluster_node_id,
        "totalProviders": total,
        "healthyProviders": healthy,
        "circuitBreakers": get_all_states(),
    }
    _HEALTH_CACHE["ts"] = now
    _HEALTH_CACHE["body"] = {k: v for k, v in body.items() if k != "circuitBreakers"}
    return body


@router.get("/cluster/status")
async def cluster_status(_: AdminUser = Depends(require_admin)):
    return get_cluster_status()


@router.post("/cluster/sync")
async def cluster_sync(request: Request, db: AsyncSession = Depends(get_db)):
    if not settings.cluster_enabled:
        raise HTTPException(403, "Cluster mode not enabled")

    body = await request.body()
    sig = request.headers.get("X-Cluster-Sig", "")
    if not verify_cluster_request(body, sig):
        raise HTTPException(403, "Invalid cluster signature")

    payload = json.loads(body)
    await apply_sync(db, payload)
    return {"ok": True}


@router.get("/cluster/oauth-pull/{provider_id}")
async def cluster_oauth_pull(
    provider_id: str, request: Request, db: AsyncSession = Depends(get_db),
):
    """v3.0.18: peer-pull endpoint for OAuth refresh-token race recovery.

    When a node loses an Anthropic/OpenAI refresh-token rotation race and
    gets ``invalid_grant``, it can fan out to peers asking "do you have
    fresher tokens for this provider?" — peers respond with their current
    OAuth state and the loser adopts the freshest one. Avoids the 24h
    auth-failure CB trip that manual re-paste used to require.

    Same HMAC-of-(node_id) auth as /cluster/settings.
    """
    if not settings.cluster_enabled:
        raise HTTPException(403, "Cluster mode not enabled")
    node_id = request.headers.get("X-Cluster-Node", "")
    sig = request.headers.get("X-Cluster-Sig", "")
    if not node_id or not verify_payload(node_id.encode(), sig):
        raise HTTPException(403, "Invalid cluster signature")

    from sqlalchemy import select
    from app.models.db import Provider
    result = await db.execute(select(Provider).where(Provider.id == provider_id))
    p = result.scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Provider not found")
    if p.provider_type not in ("claude-oauth", "codex-oauth"):
        raise HTTPException(400, f"{p.provider_type!r} is not an OAuth provider")
    if not p.api_key:
        raise HTTPException(404, "Provider has no access_token")
    return {
        "provider_id": p.id,
        "api_key": p.api_key,
        "oauth_refresh_token": p.oauth_refresh_token,
        "oauth_expires_at": p.oauth_expires_at,
        "last_user_edit_at": p.last_user_edit_at,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "extra_config": p.extra_config or {},
    }


@router.get("/cluster/settings")
async def cluster_settings(request: Request):
    """
    Returns this node's current effective settings.
    Used by peers during cluster-diff queries.
    Secured with the same HMAC shared secret as /cluster/sync.
    """
    if not settings.cluster_enabled:
        raise HTTPException(403, "Cluster mode not enabled")
    node_id = request.headers.get("X-Cluster-Node", "")
    sig = request.headers.get("X-Cluster-Sig", "")
    if not node_id or not verify_payload(node_id.encode(), sig):
        raise HTTPException(403, "Invalid cluster signature")

    s = config_runtime.settings
    result = {}
    for key, meta in config_runtime.SCHEMA.items():
        result[key] = getattr(s, key, meta["default"])
    return {"node_id": settings.cluster_node_id, "settings": result}


@router.post("/cluster/circuit-breaker/{provider_id}/reset")
async def reset_circuit_breaker(
    provider_id: str,
    _: AdminUser = Depends(require_admin),
):
    from app.routing.circuit_breaker import force_close
    await force_close(provider_id)
    return {"ok": True, "provider_id": provider_id}


@router.post("/cluster/circuit-breaker/{provider_id}/open")
async def open_circuit_breaker(
    provider_id: str,
    _: AdminUser = Depends(require_admin),
):
    from app.routing.circuit_breaker import force_open
    await force_open(provider_id)
    return {"ok": True, "provider_id": provider_id}


@router.post("/cluster/sync-now")
async def force_sync_now(
    _: AdminUser = Depends(require_admin),
):
    """v3.0.10: trigger an immediate cluster sync push to every peer.
    Normal cadence is 60s — this endpoint lets operators force
    convergence after a config change without waiting.

    Returns ``{peer_id: ok_bool, ...}`` for each reachable peer."""
    from app.cluster.manager import peers as _peers, push_sync
    from app.models.database import AsyncSessionLocal
    results = {}
    for peer_id, peer in list(_peers.items()):
        if peer.status == "unreachable":
            results[peer_id] = False
            continue
        try:
            await push_sync(peer, AsyncSessionLocal)
            results[peer_id] = True
        except Exception:
            results[peer_id] = False
    return {"pushed_to": results, "peer_count": len(_peers)}
