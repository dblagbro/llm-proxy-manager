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


_MASK = "*" * 8  # rendered for secret-typed settings when they have a value


@router.get("")
async def get_settings(
    _user: AdminUser = Depends(require_admin),
):
    """Return current effective values. Secret-typed fields are masked
    (still show whether they're set, but not the value)."""
    defaults = config_runtime.get_defaults()
    from app.config import settings as s
    result = {}
    for key, meta in config_runtime.SCHEMA.items():
        if hasattr(s, key):
            val = getattr(s, key)
        else:
            val = defaults[key]
        if meta.get("secret") and val:
            result[key] = _MASK  # keep clients honest: we never echo a secret back
        else:
            result[key] = val
    return result


@router.get("/schema")
async def get_schema(_user: AdminUser = Depends(require_admin)):
    """Render-ready metadata for the settings UI: types, labels, groupings,
    help text, and a 'secret' flag that tells the UI to use a password input."""
    out = []
    for key, meta in config_runtime.SCHEMA.items():
        out.append({
            "key": key,
            "type": meta["type"],
            "label": meta.get("label", key),
            "group": meta.get("group", "General"),
            "help": meta.get("help"),
            "secret": bool(meta.get("secret", False)),
            "default": meta["default"],
        })
    return out


@router.put("")
async def put_settings(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _user: AdminUser = Depends(require_admin),
):
    unknown = [k for k in body if k not in config_runtime.SCHEMA]
    if unknown:
        raise HTTPException(400, f"Unknown setting keys: {unknown}")
    # Drop masked secrets: the UI round-trips the "********" placeholder when
    # the user doesn't change a password/secret field; writing that back would
    # corrupt the stored value. Dropping preserves the existing value.
    cleaned = {
        k: v for k, v in body.items()
        if not (config_runtime.SCHEMA[k].get("secret") and v == _MASK)
    }
    if not cleaned:
        return {"saved": []}
    await config_runtime.save(db, cleaned)
    logger.info("settings_updated keys=%s", list(cleaned.keys()))

    # Kick off an immediate sync to peers so they pick up the change within seconds
    if settings.cluster_enabled:
        import asyncio
        from app.cluster.manager import peers as cluster_peers, push_sync
        from app.models.database import AsyncSessionLocal
        for peer in list(cluster_peers.values()):
            if peer.status != "unreachable":
                asyncio.create_task(push_sync(peer, AsyncSessionLocal))

    return {"saved": list(body.keys())}


@router.get("/cluster-diff")
async def cluster_diff(_user: AdminUser = Depends(require_admin)):
    """
    Query all peer nodes for their current settings and compare with local.
    Only available when cluster_enabled=True.
    """
    if not settings.cluster_enabled:
        return {"cluster_enabled": False, "peers": []}

    from app.cluster.manager import peers as cluster_peers, sign_payload

    # Local settings
    s = config_runtime.settings
    local_settings = {k: getattr(s, k, meta["default"]) for k, meta in config_runtime.SCHEMA.items()}
    node_id = settings.cluster_node_id or "local"
    sig = sign_payload(node_id.encode())
    headers = {"X-Cluster-Node": node_id, "X-Cluster-Sig": sig}

    peers_result = []
    for peer in cluster_peers.values():
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
