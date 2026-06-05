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
from app.utils.timefmt import utc_iso

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
        # Re-evaluate CB state + pool snapshot on every call; only the
        # provider count is cached. v3.10.3 — the cache-hit path
        # previously re-added only ``circuitBreakers``, so ``dbPool`` was
        # absent on every cache hit (i.e. ~2 of every 3s window). Both
        # are excluded from the cached body (line below) precisely so
        # they stay live — both must therefore be re-added here.
        cached = _HEALTH_CACHE["body"]
        return {
            **cached,
            "circuitBreakers": get_all_states(),
            "dbPool": _db_pool_snapshot(),
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
        # v3.9.8 — pool diagnostics. Surfaces SQLAlchemy QueuePool state
        # so operators can spot leaks without having to exec into the
        # container. Surfaced after the 2026-05-14 www01 pool-exhaustion
        # incident, which took 13h to manifest and was diagnosed by
        # running ``engine.pool.checkedout()`` inside the container —
        # making this visible at the health endpoint eliminates that step.
        # The signals to watch:
        #   checked_out climbing monotonically over hours = slow leak
        #   overflow > 0 = pool is saturated and burning overflow budget
        #   waited > 0 (cumulative) on subsequent calls = checkouts blocked
        "dbPool": _db_pool_snapshot(),
    }
    _HEALTH_CACHE["ts"] = now
    _HEALTH_CACHE["body"] = {k: v for k, v in body.items() if k not in ("circuitBreakers", "dbPool")}
    return body


def _db_pool_snapshot() -> dict:
    """Snapshot of SQLAlchemy QueuePool state. Best-effort — never raises."""
    try:
        from app.models.database import engine
        pool = engine.pool
        # checkedout() and overflow() are public; size() is the configured
        # base pool_size. Some pool implementations (NullPool, etc) don't
        # have all three; guard each.
        snap = {
            "size": pool.size() if hasattr(pool, "size") else None,
            "checked_out": pool.checkedout() if hasattr(pool, "checkedout") else None,
            "overflow": pool.overflow() if hasattr(pool, "overflow") else None,
        }
        # Convenience: total connections currently held by app
        if snap["checked_out"] is not None and snap["overflow"] is not None:
            snap["in_use"] = snap["checked_out"]
            snap["max"] = (snap["size"] or 0) + max(0, snap["overflow"])
        # v3.10.2 (ARCH-A) — checkout-tracer summary when enabled. Full
        # per-connection acquisition stacks at GET /cluster/db-pool-trace.
        # v4.4.22 — async-side session-tracer summary (the one whose
        # stacks reach app code; sync side stops at SQLA internals
        # because of the greenlet boundary).
        if getattr(settings, "db_pool_trace", False):
            from app.models.database import (
                get_pool_checkout_trace, get_async_session_trace,
            )
            trace = get_pool_checkout_trace()
            snap["trace_enabled"] = True
            snap["traced_checked_out"] = len(trace)
            snap["oldest_checkout_age_sec"] = trace[0]["age_sec"] if trace else 0.0
            async_trace = get_async_session_trace()
            snap["traced_async_sessions"] = len(async_trace)
            snap["oldest_async_session_age_sec"] = (
                async_trace[0]["age_sec"] if async_trace else 0.0
            )
        return snap
    except Exception as e:
        return {"error": str(e)[:200]}


@router.get("/cluster/status")
async def cluster_status(_: AdminUser = Depends(require_admin)):
    return get_cluster_status()


@router.get("/cluster/db-pool-trace")
async def cluster_db_pool_trace(_: AdminUser = Depends(require_admin)):
    """v3.10.2 (ARCH-A) — per-connection acquisition stacks for every
    pooled DB connection currently checked out, oldest first.

    v4.4.22 — also returns ``async_sessions``: stacks captured on the
    async side (``AsyncSession.__aenter__``) where the app caller IS
    in the frame chain. Read THIS list to identify the leaking code
    path — the sync ``checked_out`` list dead-ends at SQLAlchemy
    internals because the greenlet boundary clips the async caller.

    Returns empty lists when ``db_pool_trace`` is off.
    """
    if not getattr(settings, "db_pool_trace", False):
        return {
            "trace_enabled": False,
            "count": 0,
            "checked_out": [],
            "async_sessions": [],
            "hint": "set DB_POOL_TRACE=1 on this node and recreate the container",
        }
    from app.models.database import (
        get_pool_checkout_trace, get_async_session_trace,
    )
    trace = get_pool_checkout_trace()
    async_trace = get_async_session_trace()
    return {
        "trace_enabled": True,
        "count": len(trace),
        "checked_out": trace,
        "async_sessions": async_trace,
    }


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
    if p.provider_type not in ("claude-oauth", "ChatGPT-oauth-plan"):
        raise HTTPException(400, f"{p.provider_type!r} is not an OAuth provider")
    if not p.api_key:
        raise HTTPException(404, "Provider has no access_token")
    return {
        "provider_id": p.id,
        "api_key": p.api_key,
        "oauth_refresh_token": p.oauth_refresh_token,
        "oauth_expires_at": p.oauth_expires_at,
        "last_user_edit_at": p.last_user_edit_at,
        "updated_at": utc_iso(p.updated_at),
        "extra_config": p.extra_config or {},
    }


@router.get("/cluster/local-metrics")
async def cluster_local_metrics(
    request: Request,
    hours: int = 24,
    db: AsyncSession = Depends(get_db),
):
    """v4.4.21 — peer-pull endpoint for per-node Provider Summary.

    ``provider_metrics`` is NOT cluster-replicated (one row per
    bucket_ts on each node), so the existing
    ``/api/monitoring/metrics`` shows only the local node's own
    traffic. The Provider Summary UI needs cross-node visibility so
    the operator can see whether load is balanced across the fleet —
    e.g. "did www1 serve 95% of OpenRouter calls while c1conv served
    5%?" — without SSHing to each node.

    Same HMAC-of(node_id) auth as ``/cluster/oauth-pull`` and
    ``/cluster/settings``. Read-only, cheap (~50ms aggregate query).
    The fan-out wrapper lives at ``/api/monitoring/metrics-by-node``.
    """
    if not settings.cluster_enabled:
        raise HTTPException(403, "Cluster mode not enabled")
    node_id = request.headers.get("X-Cluster-Node", "")
    sig = request.headers.get("X-Cluster-Sig", "")
    if not node_id or not verify_payload(node_id.encode(), sig):
        raise HTTPException(403, "Invalid cluster signature")
    # Cap to the same range the admin endpoint allows (30d).
    hours = max(1, min(int(hours), 720))
    from app.monitoring.metrics import get_all_provider_summary
    summary = await get_all_provider_summary(db, hours=hours)
    return {
        "node_id": settings.cluster_node_id or "",
        "hours": hours,
        "providers": summary,
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


# ── v5.0.18 — UI-configurable cluster peer list ──────────────────────


@router.get("/cluster/peers")
async def list_cluster_peers(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """List active + tombstoned peers from the cluster_peers table.

    Active = ``removed_at IS NULL``; tombstoned rows are surfaced so
    the operator can see recent removals (and an admin could later
    "restore" by clearing the timestamp — not exposed in this round).
    """
    from sqlalchemy import select
    from app.models.db import ClusterPeer
    rs = await db.execute(select(ClusterPeer).order_by(ClusterPeer.added_at))
    rows = rs.scalars().all()
    return [
        {
            "id": r.id,
            "url": r.url,
            "name": r.name,
            "added_at": r.added_at.isoformat() if r.added_at else None,
            "removed_at": r.removed_at.isoformat() if r.removed_at else None,
            "active": r.removed_at is None,
        }
        for r in rows
    ]


from pydantic import BaseModel, Field as _Field


class _PeerCreate(BaseModel):
    id: str = _Field(..., min_length=1, max_length=64)
    url: str = _Field(..., min_length=1, max_length=512)
    name: str | None = None


@router.post("/cluster/peers")
async def add_cluster_peer(
    body: _PeerCreate,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Add a new peer (or restore a tombstoned one) by id+url. The
    next sync round (≤60s) replicates the new row to all currently-
    known peers, and the local manager's peer-refresh loop picks it
    up within 30s. Validation: rejects ids that match this node's
    own CLUSTER_NODE_ID (a node can't peer with itself)."""
    import time as _t
    from datetime import datetime
    from sqlalchemy import select
    from app.models.db import ClusterPeer
    if body.id == (settings.cluster_node_id or ""):
        raise HTTPException(400, "cannot add self as peer")
    if "://" not in body.url:
        raise HTTPException(400, "url must include scheme (http:// or https://)")
    existing = (await db.execute(
        select(ClusterPeer).where(ClusterPeer.id == body.id).limit(1)
    )).scalar_one_or_none()
    now_dt = datetime.utcnow()
    now_ts = _t.time()
    if existing is None:
        db.add(ClusterPeer(
            id=body.id, url=body.url, name=body.name,
            added_at=now_dt,
            last_user_edit_at=now_ts,
        ))
    else:
        # Restore or update an existing row (clears tombstone too).
        existing.url = body.url
        if body.name is not None:
            existing.name = body.name
        existing.removed_at = None
        existing.last_user_edit_at = now_ts
    await db.commit()
    return {"ok": True, "id": body.id, "url": body.url}


@router.delete("/cluster/peers/{peer_id}")
async def remove_cluster_peer(
    peer_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Soft-delete a peer (sets ``removed_at`` + bumps
    ``last_user_edit_at``). Replicated as a tombstone via cluster
    sync; the local manager stops pushing to it within 30s."""
    import time as _t
    from datetime import datetime
    from sqlalchemy import select
    from app.models.db import ClusterPeer
    row = (await db.execute(
        select(ClusterPeer).where(ClusterPeer.id == peer_id).limit(1)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "peer not found")
    row.removed_at = datetime.utcnow()
    row.last_user_edit_at = _t.time()
    await db.commit()
    return {"ok": True, "id": peer_id, "removed_at": row.removed_at.isoformat()}
