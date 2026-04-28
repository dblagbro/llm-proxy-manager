"""Cluster data sync — incoming payload merge logic.

Handles the insert-if-missing / update-if-changed strategy for users, API keys,
providers, and settings received from peer nodes during cluster synchronisation.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.db import User, ApiKey, Provider, SystemSetting

logger = logging.getLogger(__name__)

# Per-peer cost accumulator: {peer_node_id: {key_id: total_cost_usd}}
# Imported by manager.py and auth/keys.py for global spending-cap enforcement.
_peer_key_costs: dict[str, dict[str, float]] = {}


def get_peer_total_cost(key_id: str) -> float:
    """Sum of total_cost_usd reported by all peers for a given key."""
    return sum(costs.get(key_id, 0.0) for costs in _peer_key_costs.values())


async def apply_sync(db: AsyncSession, payload: dict) -> None:
    """Merge incoming user/key/provider/settings data from a peer (insert-if-missing strategy)."""
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

    source_node = payload.get("source_node", "unknown")
    peer_costs: dict[str, float] = {}

    for k_data in payload.get("api_keys", []):
        result = await db.execute(select(ApiKey).where(ApiKey.key_hash == k_data["key_hash"]))
        existing = result.scalar_one_or_none()
        if existing:
            if "spending_cap_usd" in k_data:
                existing.spending_cap_usd = k_data["spending_cap_usd"]
            if "rate_limit_rpm" in k_data:
                existing.rate_limit_rpm = k_data["rate_limit_rpm"]
        else:
            db.add(ApiKey(
                id=k_data["id"],
                name=k_data["name"],
                key_hash=k_data["key_hash"],
                key_prefix=k_data["key_prefix"],
                key_type=k_data.get("key_type", "standard"),
                enabled=k_data.get("enabled", True),
                spending_cap_usd=k_data.get("spending_cap_usd"),
                rate_limit_rpm=k_data.get("rate_limit_rpm"),
            ))
        key_id = k_data.get("id")
        if key_id and "total_cost_usd" in k_data:
            peer_costs[key_id] = float(k_data["total_cost_usd"])

    _peer_key_costs[source_node] = peer_costs

    from app.monitoring.status import register_provider
    from datetime import datetime
    def _parse_iso(v):
        if not v:
            return None
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return None
        return v

    for p_data in payload.get("providers", []):
        peer_deleted_at = _parse_iso(p_data.get("deleted_at"))
        peer_updated_at = _parse_iso(p_data.get("updated_at"))

        result = await db.execute(select(Provider).where(Provider.id == p_data["id"]))
        existing = result.scalar_one_or_none()
        if existing is None:
            # Match by name as a fallback (legacy rows synced pre-v2.8.2 may
            # have different ids on each node).
            result2 = await db.execute(select(Provider).where(Provider.name == p_data["name"]))
            existing = result2.scalar_one_or_none()

        if existing is not None:
            # v2.8.2: tombstone-aware merge.
            local_updated = existing.updated_at
            local_deleted = existing.deleted_at

            # If peer has a tombstone and it's newer than our local state,
            # propagate the soft-delete locally.
            if peer_deleted_at and (
                local_updated is None or peer_deleted_at >= local_updated
            ):
                existing.deleted_at = peer_deleted_at
                existing.enabled = False
                if peer_updated_at:
                    existing.updated_at = peer_updated_at
                continue

            # If WE have a tombstone newer than the peer's update, do nothing
            # — local delete wins until peer sees our tombstone next sync.
            if local_deleted and (
                peer_updated_at is None or local_deleted >= peer_updated_at
            ):
                continue

            # Standard last-write-wins merge for active rows.
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

        # No local row — create unless peer is sending a tombstone (no point
        # materializing a deleted row).
        if peer_deleted_at is not None:
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

    # Merge settings — last-write-wins by updated_at timestamp
    from app import config_runtime
    settings_to_apply: dict = {}
    for s_data in payload.get("settings", []):
        key = s_data.get("key", "")
        if key not in config_runtime.SCHEMA:
            continue
        incoming_ts = float(s_data.get("updated_at", 0))
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        existing = result.scalar_one_or_none()
        if existing and (existing.updated_at or 0) >= incoming_ts:
            continue
        if existing:
            existing.value = s_data["value"]
            existing.value_type = s_data.get("value_type", "str")
            existing.updated_at = incoming_ts
        else:
            db.add(SystemSetting(
                key=key,
                value=s_data["value"],
                value_type=s_data.get("value_type", "str"),
                updated_at=incoming_ts,
            ))
        settings_to_apply[key] = config_runtime._coerce(s_data["value"], s_data.get("value_type", "str"))

    # v2.8.2: normalize any priority ties introduced by the merge so every
    # node converges on the same strict total order. Deterministic by
    # (priority, created_at, id) — all peers arrive at the same answer.
    try:
        from app.api.providers import normalize_priority_ties
        bumped = await normalize_priority_ties(db)
        if bumped:
            logger.info("cluster_sync_normalized_ties count=%s", bumped)
    except Exception:
        logger.exception("priority-tie normalization failed during sync apply")

    await db.commit()

    if settings_to_apply:
        config_runtime.apply(settings_to_apply)
        logger.info("cluster_settings_applied count=%s keys=%s", len(settings_to_apply), list(settings_to_apply))
