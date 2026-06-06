"""v5.1.2 / Batch C3 — Activity-log retention editable from the WebUI.

Pre-feature: retention values were env-only
(``ACTIVITY_LOG_{INFO,WARNING,ERROR}_RETENTION_DAYS``). Operators
who needed to shorten retention for compliance had to redeploy.

This module adds a ``system_settings``-backed override layer:

  - Three keys in system_settings (cluster-synced):
      ``compliance.activity_log_retention_days``
      ``compliance.activity_log_warning_retention_days``
      ``compliance.activity_log_error_retention_days``
  - Module-level cache so the sync prune helpers can read without
    awaiting (they get called from an async sweep loop but the helper
    signatures themselves are sync — preserving v3.0.7 contract).
  - ``refresh_from_db()`` re-reads all three keys. Called:
      - On app startup (lifespan hook)
      - After every admin write (toggle/retention endpoint)
      - Before each daily prune sweep (so a flip lands within one
        sweep cycle)
  - If the override row is absent OR malformed, falls back to the
    env-configured value from ``app.config.settings``.

Audit: every retention edit writes a ``compliance_policy_changes``
row with scope='system'. Itself NEVER purgeable.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# system_settings keys. Named with the ``compliance.`` prefix so they
# group with the C1 toggle key in any future Settings UI listing.
KEY_INFO    = "compliance.activity_log_retention_days"
KEY_WARNING = "compliance.activity_log_warning_retention_days"
KEY_ERROR   = "compliance.activity_log_error_retention_days"

ALL_KEYS = (KEY_INFO, KEY_WARNING, KEY_ERROR)

# Module-level cache. Each value is either an explicit int (operator
# override) or None (no override — use env default).
_cache: dict[str, Optional[int]] = {k: None for k in ALL_KEYS}
_cache_at: float = 0.0
# Soft TTL — readers tolerate up to 60s of staleness; on hot paths a
# refresh_from_db() call invalidates immediately. The prune sweep
# itself calls refresh just before reading.
_CACHE_TTL_SEC = 60.0


def _env_default(key: str) -> int:
    """Fall-back to the env-derived value from settings."""
    from app.config import settings
    if key == KEY_INFO:
        return int(getattr(settings, "activity_log_retention_days", 30) or 30)
    if key == KEY_WARNING:
        return int(getattr(settings, "activity_log_warning_retention_days", 365) or 365)
    if key == KEY_ERROR:
        return int(getattr(settings, "activity_log_error_retention_days", 1825) or 1825)
    return 30


def _coerce_int_or_none(raw: Optional[str]) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    if v < 1:
        return None
    return v


async def refresh_from_db(db: AsyncSession) -> None:
    """Re-read all three retention keys from system_settings into the
    cache. Best-effort — errors leave the cache as-is."""
    global _cache, _cache_at
    try:
        from app.models.db import SystemSetting
        rs = await db.execute(
            select(SystemSetting).where(SystemSetting.key.in_(ALL_KEYS))
        )
        rows = rs.scalars().all()
        seen = {r.key: _coerce_int_or_none(r.value) for r in rows}
        for k in ALL_KEYS:
            _cache[k] = seen.get(k)  # None if absent
        _cache_at = time.time()
    except Exception as exc:
        logger.warning("retention_settings.refresh_failed err=%r", exc)


def get_retention_days(key: str) -> int:
    """Sync getter used by the prune helpers. Returns the operator
    override if present, else the env default."""
    if key not in ALL_KEYS:
        raise ValueError(f"unknown retention key: {key}")
    cached = _cache.get(key)
    if cached is not None:
        return max(1, cached)
    return _env_default(key)


# Convenience accessors mirroring prune.py's existing helpers
def info_days() -> int:    return get_retention_days(KEY_INFO)
def warning_days() -> int: return get_retention_days(KEY_WARNING)
def error_days() -> int:   return get_retention_days(KEY_ERROR)


def current_state() -> dict:
    """For the admin status endpoint — surfaces both the override (or
    None) and the resolved effective value per key."""
    state = {}
    for k in ALL_KEYS:
        state[k] = {
            "override": _cache.get(k),
            "env_default": _env_default(k),
            "effective_days": get_retention_days(k),
        }
    return state


async def set_retention(
    db: AsyncSession, key: str, days: Optional[int],
    actor: str, reason: Optional[str] = None,
) -> dict:
    """Persist a retention override + write a system-scope audit row +
    refresh the cache. ``days=None`` clears the override (falls back
    to env default)."""
    import json as _json
    import secrets
    from datetime import datetime
    from app.models.db import SystemSetting, CompliancePolicyChange

    if key not in ALL_KEYS:
        raise ValueError(f"unknown retention key: {key}")
    if days is not None and days < 1:
        raise ValueError("days must be >= 1 (or None to clear the override)")
    if days is not None and days > 36500:
        # 100 years — defensive upper bound; an operator typing too
        # many digits shouldn't accidentally disable pruning forever.
        raise ValueError("days must be <= 36500 (100 years)")

    # 1) Read prior for the audit row
    prior_override = _cache.get(key)
    prior_effective = get_retention_days(key)

    # 2) Upsert / delete the system_settings row
    rs = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = rs.scalar_one_or_none()
    if days is None:
        # Clear: delete the row if it exists.
        if row is not None:
            await db.delete(row)
    else:
        new_value = str(int(days))
        if row is None:
            db.add(SystemSetting(
                key=key, value=new_value, value_type="int",
                updated_at=time.time(),
            ))
        else:
            row.value = new_value
            row.updated_at = time.time()

    # 3) Audit row in compliance_policy_changes
    audit_id = secrets.token_urlsafe(16)
    summary = (
        f"activity_log retention edited: {key} prior_override={prior_override} "
        f"new_override={days} actor={actor}"
        + (f"; reason: {reason}" if reason else "")
    )
    db.add(CompliancePolicyChange(
        policy_change_id=audit_id,
        changed_at=datetime.utcnow(),
        changed_by_user_id=actor,
        scope="system",
        target_id=None,
        before_state=_json.dumps({"key": key, "override": prior_override,
                                  "effective": prior_effective}),
        after_state=_json.dumps({"key": key, "override": days,
                                 "effective_will_be": days if days else _env_default(key)}),
        reason=summary,
        applied_to_peers=_json.dumps([]),
        pending_peers=None,
        cluster_sync_status="local_only",
    ))
    await db.commit()

    # 4) Refresh the cache so the new value takes effect on the next
    #    prune sweep without waiting for the TTL.
    await refresh_from_db(db)

    logger.warning(
        "retention_settings.set key=%s days=%s actor=%s",
        key, days, actor,
    )
    return {
        "ok": True, "key": key,
        "prior_override": prior_override,
        "new_override": days,
        "effective_days": get_retention_days(key),
        "audit_id": audit_id,
    }
