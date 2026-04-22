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
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models.db import User, ApiKey, Provider

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


_peers: dict[str, PeerNode] = {}
_heartbeat_task: Optional[asyncio.Task] = None
_sync_task: Optional[asyncio.Task] = None


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


def _sign(payload: bytes) -> str:
    key = (settings.cluster_sync_secret or "").encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _verify(payload: bytes, signature: str) -> bool:
    expected = _sign(payload)
    return hmac.compare_digest(expected, signature)


def _auth_headers(payload: dict) -> dict:
    body = json.dumps(payload, sort_keys=True).encode()
    return {
        "X-Cluster-Node": settings.cluster_node_id or "",
        "X-Cluster-Sig": _sign(body),
        "Content-Type": "application/json",
    }


def verify_cluster_request(body: bytes, signature: str) -> bool:
    return _verify(body, signature)


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
                await _push_sync(peer, db_factory)


async def _push_sync(peer: PeerNode, db_factory):
    async with db_factory() as db:
        users_result = await db.execute(select(User))
        users = [
            {"id": u.id, "username": u.username, "password_hash": u.password_hash,
             "role": u.role, "created_at": str(u.created_at)}
            for u in users_result.scalars().all()
        ]
        keys_result = await db.execute(select(ApiKey))
        keys = [
            {"id": k.id, "name": k.name, "key_hash": k.key_hash, "key_prefix": k.key_prefix,
             "key_type": k.key_type, "enabled": k.enabled}
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

    payload = {
        "source_node": settings.cluster_node_id,
        "timestamp": time.time(),
        "users": users,
        "api_keys": keys,
        "providers": providers,
    }
    body = json.dumps(payload, sort_keys=True).encode()
    sig = _sign(body)

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


async def apply_sync(db: AsyncSession, payload: dict):
    """Merge incoming user/key/provider data from a peer (insert-if-missing strategy)."""
    for u_data in payload.get("users", []):
        result = await db.execute(select(User).where(User.username == u_data["username"]))
        existing = result.scalar_one_or_none()
        if not existing:
            db.add(User(
                id=u_data["id"],
                username=u_data["username"],
                password_hash=u_data["password_hash"],
                role=u_data.get("role", "user"),
            ))

    for k_data in payload.get("api_keys", []):
        result = await db.execute(select(ApiKey).where(ApiKey.key_hash == k_data["key_hash"]))
        existing = result.scalar_one_or_none()
        if not existing:
            db.add(ApiKey(
                id=k_data["id"],
                name=k_data["name"],
                key_hash=k_data["key_hash"],
                key_prefix=k_data["key_prefix"],
                key_type=k_data.get("key_type", "standard"),
                enabled=k_data.get("enabled", True),
            ))

    from app.monitoring.status import register_provider
    for p_data in payload.get("providers", []):
        # Check by ID first (exact match)
        result = await db.execute(select(Provider).where(Provider.id == p_data["id"]))
        existing = result.scalar_one_or_none()
        if existing:
            # Update config fields so changes on one node propagate
            existing.api_key = p_data.get("api_key", existing.api_key)
            existing.base_url = p_data.get("base_url", existing.base_url)
            existing.default_model = p_data.get("default_model", existing.default_model)
            existing.priority = p_data.get("priority", existing.priority)
            existing.enabled = p_data.get("enabled", existing.enabled)
            existing.timeout_sec = p_data.get("timeout_sec", existing.timeout_sec)
            existing.exclude_from_tool_requests = p_data.get("exclude_from_tool_requests", existing.exclude_from_tool_requests)
            existing.hold_down_sec = p_data.get("hold_down_sec", existing.hold_down_sec)
            existing.failure_threshold = p_data.get("failure_threshold", existing.failure_threshold)
            existing.extra_config = p_data.get("extra_config", existing.extra_config)
            continue
        # Check by name — update existing entry if same name under a different ID
        result2 = await db.execute(select(Provider).where(Provider.name == p_data["name"]))
        existing_by_name = result2.scalar_one_or_none()
        if existing_by_name:
            existing_by_name.api_key = p_data.get("api_key", existing_by_name.api_key)
            existing_by_name.base_url = p_data.get("base_url", existing_by_name.base_url)
            existing_by_name.default_model = p_data.get("default_model", existing_by_name.default_model)
            existing_by_name.priority = p_data.get("priority", existing_by_name.priority)
            existing_by_name.enabled = p_data.get("enabled", existing_by_name.enabled)
            existing_by_name.timeout_sec = p_data.get("timeout_sec", existing_by_name.timeout_sec)
            existing_by_name.exclude_from_tool_requests = p_data.get("exclude_from_tool_requests", existing_by_name.exclude_from_tool_requests)
            existing_by_name.hold_down_sec = p_data.get("hold_down_sec", existing_by_name.hold_down_sec)
            existing_by_name.failure_threshold = p_data.get("failure_threshold", existing_by_name.failure_threshold)
            existing_by_name.extra_config = p_data.get("extra_config", existing_by_name.extra_config)
            continue
        p = Provider(
            id=p_data["id"],
            name=p_data["name"],
            provider_type=p_data["provider_type"],
            api_key=p_data.get("api_key"),
            base_url=p_data.get("base_url"),
            default_model=p_data.get("default_model"),
            priority=p_data.get("priority", 10),
            enabled=p_data.get("enabled", True),
            timeout_sec=p_data.get("timeout_sec", 60),
            exclude_from_tool_requests=p_data.get("exclude_from_tool_requests", False),
            hold_down_sec=p_data.get("hold_down_sec"),
            failure_threshold=p_data.get("failure_threshold"),
            extra_config=p_data.get("extra_config", {}),
        )
        db.add(p)
        register_provider(p.id, p.provider_type, p.hold_down_sec, p.failure_threshold)

    await db.commit()


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
