"""
GET  /api/settings         — return current effective values (DB overrides + env defaults)
PUT  /api/settings         — persist a partial update and apply live
GET  /api/settings/cluster-diff — compare settings across all cluster nodes
"""
import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.auth.admin import require_admin, AdminUser
from app.config import settings
from app import config_runtime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def get_settings(
    _user: AdminUser = Depends(require_admin),
):
    defaults = config_runtime.get_defaults()
    # Overlay with whatever is currently live on the settings singleton
    from app.config import settings as s
    result = {}
    for key, meta in config_runtime.SCHEMA.items():
        if hasattr(s, key):
            result[key] = getattr(s, key)
        else:
            result[key] = defaults[key]
    return result


@router.put("")
async def put_settings(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _user: AdminUser = Depends(require_admin),
):
    unknown = [k for k in body if k not in config_runtime.SCHEMA]
    if unknown:
        raise HTTPException(400, f"Unknown setting keys: {unknown}")
    await config_runtime.save(db, body)
    logger.info("settings_updated", keys=list(body.keys()))

    # Kick off an immediate sync to peers so they pick up the change within seconds
    if settings.cluster_enabled:
        import asyncio
        from app.cluster.manager import _peers, _push_sync
        from app.models.database import AsyncSessionLocal
        for peer in list(_peers.values()):
            if peer.status != "unreachable":
                asyncio.create_task(_push_sync(peer, AsyncSessionLocal))

    return {"saved": list(body.keys())}


@router.get("/cluster-diff")
async def cluster_diff(_user: AdminUser = Depends(require_admin)):
    """
    Query all peer nodes for their current settings and compare with local.
    Only available when cluster_enabled=True.
    """
    if not settings.cluster_enabled:
        return {"cluster_enabled": False, "peers": []}

    from app.cluster.manager import _peers, _sign

    # Local settings
    s = config_runtime.settings
    local_settings = {k: getattr(s, k, meta["default"]) for k, meta in config_runtime.SCHEMA.items()}
    node_id = settings.cluster_node_id or "local"
    sig = _sign(node_id.encode())
    headers = {"X-Cluster-Node": node_id, "X-Cluster-Sig": sig}

    peers_result = []
    for peer in _peers.values():
        if peer.status == "unreachable":
            peers_result.append({"id": peer.id, "name": peer.name, "status": "unreachable", "settings": None, "diffs": []})
            continue
        try:
            async with httpx.AsyncClient(timeout=8, verify=False) as client:
                resp = await client.get(f"{peer.url.rstrip('/')}/cluster/settings", headers=headers)
            if resp.status_code != 200:
                peers_result.append({"id": peer.id, "name": peer.name, "status": "error", "settings": None, "diffs": []})
                continue
            peer_data = resp.json()
            peer_settings = peer_data.get("settings", {})
            diffs = [
                k for k in local_settings
                if k in peer_settings and str(local_settings[k]) != str(peer_settings[k])
            ]
            peers_result.append({
                "id": peer.id,
                "name": peer.name,
                "status": peer.status,
                "settings": peer_settings,
                "diffs": diffs,
            })
        except Exception as e:
            peers_result.append({"id": peer.id, "name": peer.name, "status": "error", "settings": None, "diffs": [], "error": str(e)})

    all_synced = all(len(p["diffs"]) == 0 for p in peers_result if p["settings"] is not None)
    return {
        "cluster_enabled": True,
        "local": {"node_id": node_id, "settings": local_settings},
        "peers": peers_result,
        "all_synced": all_synced,
    }
