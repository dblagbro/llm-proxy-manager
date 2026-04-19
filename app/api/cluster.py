"""Cluster coordination endpoints."""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.auth.admin import require_admin, AdminUser
from app.cluster.manager import get_cluster_status, apply_sync, verify_cluster_request
from app.routing.circuit_breaker import get_all_states
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["cluster"])


@router.get("/health")
async def health():
    """Public health endpoint — also used by cluster peers for heartbeat."""
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider
    from sqlalchemy import select
    from app.routing.circuit_breaker import is_available

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Provider).where(Provider.enabled == True))
        providers = result.scalars().all()

    total = len(providers)
    healthy = sum(1 for p in providers if await is_available(p.id))

    return {
        "status": "healthy" if healthy > 0 else "degraded",
        "version": "2.0.0",
        "nodeId": settings.cluster_node_id,
        "totalProviders": total,
        "healthyProviders": healthy,
        "circuitBreakers": get_all_states(),
    }


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
