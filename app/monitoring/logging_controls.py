"""v5.1.0 / Batch C1 — Activity-log on/off toggle (compliance panic button).

When proxy traffic carries PII, the operator needs to be able to halt
activity-log capture without redeploying. This module provides:

  - ``is_logging_enabled()`` — module-cached read of the
    ``compliance.activity_logging_enabled`` system_setting. Default ON
    (pre-feature behavior preserved).
  - ``set_logging_enabled(db, enabled, actor, reason)`` — flips the
    persistent setting AND writes an audit row to compliance_events.
    Invalidates the in-process cache so the change takes effect on the
    next ``log_event`` call.
  - ``invalidate_cache()`` — re-reads from DB on next check.

The toggle is intentionally a single ON/OFF switch — no granularity by
event-type or severity. If you need a partial / time-bounded purge,
use the bulk-delete endpoint (Batch C2 — not yet shipped).

Audit rows are themselves NEVER purgeable. The compliance_events
table is the operator's proof of who flipped logging and when. The
audit_id chain hash carries over so a tamper after the fact would
break the chain on the next event.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# system_settings key. Value is "true" or "false" (stored as text).
SETTING_KEY = "compliance.activity_logging_enabled"

# Module-level cache + simple TTL so log_event's hot path doesn't hit
# the DB on every call. The toggle endpoint also clears the cache
# eagerly via invalidate_cache() so flips take effect immediately.
_cache_value: Optional[bool] = None
_cache_at: float = 0.0
_CACHE_TTL_SEC = 30.0


def invalidate_cache() -> None:
    global _cache_value, _cache_at
    _cache_value = None
    _cache_at = 0.0


async def _read_setting(db: AsyncSession) -> bool:
    """Read the persistent toggle from system_settings. Defaults True
    (logging ON) if the row doesn't exist — preserves pre-feature
    behavior on existing deployments.
    """
    from sqlalchemy import select
    from app.models.db import SystemSetting
    rs = await db.execute(select(SystemSetting).where(SystemSetting.key == SETTING_KEY))
    row = rs.scalar_one_or_none()
    if row is None:
        return True
    return (row.value or "").strip().lower() in ("true", "1", "yes", "on")


async def is_logging_enabled(db: AsyncSession) -> bool:
    """Cheap async check used by ``log_event``. TTL-cached to avoid
    hitting the DB on every event. ``invalidate_cache()`` from the
    toggle endpoint clears the cache so flips take effect at once.
    """
    global _cache_value, _cache_at
    now = time.time()
    if _cache_value is not None and (now - _cache_at) < _CACHE_TTL_SEC:
        return _cache_value
    try:
        val = await _read_setting(db)
    except Exception as exc:
        # On DB error: default to ENABLED (fail-open for observability).
        # The alternative (fail-closed silent drop) is dangerous; an
        # operator who can't see logs has no signal that something is
        # broken.
        logger.warning("logging_controls.read_failed err=%r — defaulting to enabled", exc)
        val = True
    _cache_value = val
    _cache_at = now
    return val


async def set_logging_enabled(
    db: AsyncSession,
    enabled: bool,
    actor: str,
    reason: Optional[str] = None,
) -> dict:
    """Flip the persistent setting + write an audit row + invalidate
    the cache. Returns a small status dict.

    ``actor`` should be the admin user's username. ``reason`` is
    optional free-text and is captured in the audit row's
    ``compliance_decision_summary`` field.
    """
    from datetime import datetime
    import secrets
    import hashlib
    from sqlalchemy import select
    from app.models.db import SystemSetting

    # 1) Read prior state for the audit row
    prior = await _read_setting(db)
    if prior == enabled:
        # No-op — still record the (re)affirmation for audit trail.
        new_state_str = "true" if enabled else "false"
        noop = True
    else:
        new_state_str = "true" if enabled else "false"
        noop = False

    # 2) Upsert the system_settings row
    rs = await db.execute(select(SystemSetting).where(SystemSetting.key == SETTING_KEY))
    row = rs.scalar_one_or_none()
    if row is None:
        db.add(SystemSetting(
            key=SETTING_KEY,
            value=new_state_str,
            value_type="bool",
            updated_at=time.time(),
        ))
    else:
        row.value = new_state_str
        row.updated_at = time.time()

    # 3) Audit row in compliance_policy_changes (system-scope) — the
    #    right table for a system-wide toggle. compliance_events is
    #    keyed on api_key_id (per-request audit, wrong fit here).
    #    The daily ComplianceAuditChain hash sweeper covers both
    #    tables; tamper with a closed-day's toggle row and the chain
    #    breaks.
    import json as _json
    from app.models.db import CompliancePolicyChange
    audit_id = secrets.token_urlsafe(16)
    summary = (
        f"activity_logging {'ENABLED' if enabled else 'DISABLED'} by {actor}"
        + (f"; reason: {reason}" if reason else "")
        + (" (no-op re-affirmation)" if noop else "")
    )
    db.add(CompliancePolicyChange(
        policy_change_id=audit_id,
        changed_at=datetime.utcnow(),
        changed_by_user_id=actor,
        scope="system",
        target_id=None,
        before_state=_json.dumps({"activity_logging_enabled": prior}),
        after_state=_json.dumps({"activity_logging_enabled": enabled}),
        reason=summary,
        applied_to_peers=_json.dumps([]),  # cluster sync replicates the
                                            # system_setting separately
        pending_peers=None,
        cluster_sync_status="local_only",
    ))

    await db.commit()

    # 4) Invalidate cache so next is_logging_enabled() re-reads
    invalidate_cache()

    logger.warning(
        "logging_controls.toggled enabled=%s actor=%s reason=%r noop=%s",
        enabled, actor, reason, noop,
    )
    return {
        "ok": True, "enabled": enabled,
        "prior_state": prior,
        "noop": noop,
        "audit_id": audit_id,
    }
