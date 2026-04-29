"""API key authentication."""
import hashlib
import secrets
import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.sql import func

from app.models.db import ApiKey
from app.cluster.sync import get_peer_total_cost
from app.budget.tracker import check_budget_pre_request, BudgetStatus
from app.auth.rate_limit_tiers import get_tier

# Rate-limit machinery lives in a sibling module (extracted 2026-04-23). These
# re-exports preserve the previous public surface so existing tests (which
# reach into _rpm_windows etc.) keep working unchanged.
from app.auth import rate_limit_state as _rl_state
from app.auth.rate_limit_state import (
    _rpm_windows, _rpd_buckets, _burst_counters,
    _check_rate_limit, _check_rpd, _check_burst,
    begin_in_flight, end_in_flight,
)


def active_node_count() -> int:
    """Re-export for backwards compat with tests that monkeypatch
    `app.auth.keys.active_node_count`. Forwards into rate_limit_state
    so the override is seen by _check_rate_limit / _check_rpd."""
    return _rl_state.active_node_count()

logger = logging.getLogger(__name__)


@dataclass
class ApiKeyRecord:
    id: str
    name: str
    key_type: str  # standard|claude-code
    semantic_cache_enabled: bool = False
    budget_status: Optional[BudgetStatus] = None
    rate_limit_tier: Optional[str] = None


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

    # v3.0.0-r5+ (hub-team smoke bug #2): treat any non-positive cap as
    # "unlimited". Previously ``spending_cap_usd=-1`` was compared
    # arithmetically and rejected the very first request with a "cap of
    # $-1.0 reached" 429. The convention from API consumers is that -1
    # means "no cap"; honour it here.
    if key.spending_cap_usd is not None and key.spending_cap_usd > 0:
        global_cost = (key.total_cost_usd or 0.0) + get_peer_total_cost(key.id)
        if global_cost >= key.spending_cap_usd:
            raise HTTPException(429, f"API key spending cap of ${key.spending_cap_usd:.4f} reached")

    # Wave 1 #5 — tiered budget caps (hourly burst + daily soft/hard)
    budget_status = await check_budget_pre_request(db, key)

    # Wave 6: named tier applies first, then a per-key rate_limit_rpm override
    # can tighten it further. Both checks run — most restrictive wins.
    tier = get_tier(getattr(key, "rate_limit_tier", None))
    if tier:
        if tier.rpm is not None:
            _check_rate_limit(key.id, tier.rpm)
        if tier.rpd is not None:
            _check_rpd(key.id, tier.rpd)
        if tier.burst is not None:
            _check_burst(key.id, tier.burst)

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
        rate_limit_tier=getattr(key, "rate_limit_tier", None),
    )
