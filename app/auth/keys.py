"""API key authentication."""
import collections
import hashlib
import secrets
import logging
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.sql import func

from app.models.db import ApiKey
from app.cluster.manager import active_node_count
from app.cluster.sync import get_peer_total_cost
from app.budget.tracker import check_budget_pre_request, BudgetStatus

logger = logging.getLogger(__name__)

# Sliding-window RPM tracker: key_id → deque of request timestamps
_rpm_windows: dict[str, collections.deque] = {}


def _check_rate_limit(key_id: str, limit_rpm: int) -> None:
    """Raise HTTP 429 if this node's share of limit_rpm is exceeded in the last 60 seconds."""
    nodes = max(1, active_node_count())
    per_node_limit = max(1, limit_rpm // nodes)

    now = time.monotonic()
    window = _rpm_windows.setdefault(key_id, collections.deque())
    cutoff = now - 60.0
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= per_node_limit:
        raise HTTPException(429, f"Rate limit exceeded: {limit_rpm} requests/minute (cluster-wide)", headers={"Retry-After": "60"})
    window.append(now)


@dataclass
class ApiKeyRecord:
    id: str
    name: str
    key_type: str  # standard|claude-code
    semantic_cache_enabled: bool = False
    budget_status: Optional[BudgetStatus] = None


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Returns (raw_key, key_hash). Raw key is shown once and never stored."""
    raw = "llmp-" + secrets.token_urlsafe(32)
    return raw, _hash_key(raw)


async def verify_api_key(db: AsyncSession, raw_key: Optional[str]) -> ApiKeyRecord:
    if not raw_key:
        raise HTTPException(401, "Missing API key")
    key_hash = _hash_key(raw_key)
    result = await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.enabled == True))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(401, "Invalid or disabled API key")

    if key.spending_cap_usd is not None:
        global_cost = (key.total_cost_usd or 0.0) + get_peer_total_cost(key.id)
        if global_cost >= key.spending_cap_usd:
            raise HTTPException(429, f"API key spending cap of ${key.spending_cap_usd:.4f} reached")

    # Wave 1 #5 — tiered budget caps (hourly burst + daily soft/hard)
    budget_status = await check_budget_pre_request(db, key)

    if key.rate_limit_rpm is not None:
        _check_rate_limit(key.id, key.rate_limit_rpm)

    # Update usage stats (fire-and-forget, non-blocking)
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key.id)
        .values(total_requests=ApiKey.total_requests + 1, last_used_at=func.now())
    )
    await db.commit()

    return ApiKeyRecord(
        id=key.id,
        name=key.name,
        key_type=key.key_type,
        semantic_cache_enabled=bool(key.semantic_cache_enabled),
        budget_status=budget_status,
    )
