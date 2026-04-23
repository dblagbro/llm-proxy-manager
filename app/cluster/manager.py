"""
Cluster manager — multi-node coordination.

Each node runs an identical stateless service; Redis (when available) holds
shared circuit-breaker and rate-limit state automatically. This module handles:
  - Peer heartbeat (every 30s)
  - Config sync: users + API keys pushed/pulled via HMAC-signed requests
  - Cluster health endpoint
  - Node registration on startup
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from sqlalchemy import select

from app.config import settings
from app.models.db import User, ApiKey, Provider, SystemSetting
from app.cluster.auth import sign_payload, verify_payload, verify_cluster_request, auth_headers_for
from app.cluster.sync import apply_sync, get_peer_total_cost

logger = logging.getLogger(__name__)


@dataclass
class PeerNode:
    id: str
    name: str
    url: str
    priority: int = 10
    status: str = "unknown"       # healthy|degraded|unreachable
    last_heartbeat: float = 0.0
    latency_ms: float = 0.0
    healthy_providers: int = 0
    total_providers: int = 0


peers: dict[str, PeerNode] = {}
_heartbeat_task: Optional[asyncio.Task] = None
_sync_task: Optional[asyncio.Task] = None

# Private alias for internal use within this module
_peers = peers


def active_node_count() -> int:
    """Number of nodes currently reachable, including self."""
    return 1 + sum(1 for p in _peers.values() if p.status != "unreachable")


def _parse_peers() -> list[PeerNode]:
    raw = settings.cluster_peers or ""
    nodes = []
    for item in raw.split(","):
        item = item.strip()
        if ":" not in item:
            continue
        node_id, _, url = item.partition(":")
        nodes.append(PeerNode(id=node_id.strip(), name=node_id.strip(), url=url.strip()))
    return nodes



async def _heartbeat_loop(notify_fn=None):
    while True:
        await asyncio.sleep(settings.cluster_heartbeat_sec)
        for peer in list(_peers.values()):
            await _ping_peer(peer, notify_fn)


async def _ping_peer(peer: PeerNode, notify_fn=None):
    url = f"{peer.url.rstrip('/')}/health"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(url, headers={"X-Cluster-Node": settings.cluster_node_id or ""})
        latency_ms = (time.monotonic() - start) * 1000
        data = resp.json()

        was_unreachable = peer.status == "unreachable"
        peer.latency_ms = latency_ms
        peer.last_heartbeat = time.time()
        peer.healthy_providers = data.get("healthyProviders", 0)
        peer.total_providers = data.get("totalProviders", 0)
        peer.status = data.get("status", "healthy")

        if was_unreachable:
            logger.info(f"Cluster peer {peer.id} recovered")

    except Exception as e:
        if peer.status != "unreachable":
            logger.warning(f"Cluster peer {peer.id} unreachable: {e}")
            peer.status = "unreachable"
            if notify_fn:
                await notify_fn(peer.id, peer.url)


async def _sync_loop(db_factory):
    """Push local users/keys to all peers every 60 seconds."""
    while True:
        await asyncio.sleep(60)
        for peer in list(_peers.values()):
            if peer.status != "unreachable":
                await push_sync(peer, db_factory)


async def _build_sync_payload(db) -> dict:
    users_result = await db.execute(select(User))
    users = [
        {"id": u.id, "username": u.username, "password_hash": u.password_hash,
         "role": u.role, "created_at": str(u.created_at)}
        for u in users_result.scalars().all()
    ]
    keys_result = await db.execute(select(ApiKey))
    keys = [
        {"id": k.id, "name": k.name, "key_hash": k.key_hash, "key_prefix": k.key_prefix,
         "key_type": k.key_type, "enabled": k.enabled,
         "spending_cap_usd": k.spending_cap_usd,
         "rate_limit_rpm": k.rate_limit_rpm,
         "total_cost_usd": k.total_cost_usd or 0.0}
        for k in keys_result.scalars().all()
    ]
    providers_result = await db.execute(select(Provider))
    providers = [
        {"id": p.id, "name": p.name, "provider_type": p.provider_type, "api_key": p.api_key,
         "base_url": p.base_url, "default_model": p.default_model, "priority": p.priority,
         "enabled": p.enabled, "timeout_sec": p.timeout_sec,
         "exclude_from_tool_requests": p.exclude_from_tool_requests,
         "hold_down_sec": p.hold_down_sec, "failure_threshold": p.failure_threshold,
         "extra_config": p.extra_config or {}}
        for p in providers_result.scalars().all()
    ]
    # Only push settings that were explicitly saved (have a DB row) — not env-var defaults
    settings_result = await db.execute(select(SystemSetting))
    node_settings = [
        {"key": s.key, "value": s.value, "value_type": s.value_type, "updated_at": s.updated_at or 0.0}
        for s in settings_result.scalars().all()
    ]
    return {
        "source_node": settings.cluster_node_id,
        "timestamp": time.time(),
        "users": users,
        "api_keys": keys,
        "providers": providers,
        "settings": node_settings,
    }


async def push_sync(peer: PeerNode, db_factory):
    async with db_factory() as db:
        payload = await _build_sync_payload(db)
    body = json.dumps(payload, sort_keys=True).encode()
    sig = sign_payload(body)

    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            await client.post(
                f"{peer.url.rstrip('/')}/cluster/sync",
                content=body,
                headers={"X-Cluster-Node": settings.cluster_node_id or "", "X-Cluster-Sig": sig,
                         "Content-Type": "application/json"},
            )
    except Exception as e:
        logger.warning(f"Sync to {peer.id} failed: {e}")


_push_sync = push_sync


def get_cluster_status() -> dict:
    return {
        "cluster_enabled": settings.cluster_enabled,
        "local_node": {
            "id": settings.cluster_node_id,
            "name": settings.cluster_node_name,
            "url": settings.cluster_node_url,
            "status": "healthy",
        },
        "peers": [
            {
                "id": p.id,
                "name": p.name,
                "url": p.url,
                "status": p.status,
                "latency_ms": round(p.latency_ms, 1),
                "last_heartbeat": p.last_heartbeat,
                "healthy_providers": p.healthy_providers,
                "total_providers": p.total_providers,
            }
            for p in _peers.values()
        ],
        "total_nodes": 1 + len(_peers),
        "healthy_nodes": 1 + sum(1 for p in _peers.values() if p.status == "healthy"),
    }


def start_cluster(db_factory, notify_fn=None):
    global _heartbeat_task, _sync_task
    if not settings.cluster_enabled:
        return

    for peer in _parse_peers():
        _peers[peer.id] = peer

    _heartbeat_task = asyncio.create_task(_heartbeat_loop(notify_fn))
    _sync_task = asyncio.create_task(_sync_loop(db_factory))
    logger.info(f"Cluster started — {len(_peers)} peers registered")
