"""v5.2.0 / Batch V1 — LLM-call emergency stop (kill switch).

DIFFERENT FROM the v5.1.0 ``activity_logging_enabled`` toggle. That
one stops *activity log writes*. This one stops *LLM calls*: when
enabled, every ``/v1/messages``, ``/v1/chat/completions``, and every
background ``acompletion_with_retry`` invocation aborts with 503 +
audit row, without dispatching upstream.

Use cases (operator-facing):

* Compliance incident — we just learned a tenant's key is exfiltrating
  data; halt routing fleet-wide while we investigate, instead of
  rolling per-key blocklist edits to every node.
* Vendor outage cascade — every fallback in a region is failing and
  the retries are amplifying load; stop the world.
* Migration cutover — point all consumers at a new endpoint and want
  to be sure none of them are still hitting this proxy mid-cutover.

Semantics:

  - System-wide. NOT per-key. The per-key blocklist (v5.0.0) is the
    targeted mechanism; this is the master switch.
  - Default OFF. New deployments behave exactly like pre-v5.2.0.
  - Replication: stored in ``system_settings`` so the existing
    cluster-sync loop fans the flip to peers within one round (~60s).
  - TTL cache 30s — matches ``logging_controls``. Cache invalidates
    eagerly on the toggle endpoint; peers converge via TTL.
  - Fail-OPEN on DB error. An operator who can't see whether the kill
    switch is ON has a worse mode (silent traffic drop) than one who
    sees their kill switch ineffectual. The toggle endpoint + UI panel
    are the operator's surface for verification.
  - Audit row per flip: ``compliance_policy_changes`` scope='system'.
  - Audit row per blocked request: ``compliance_events`` with
    ``reason_code='llm-emergency-stop'``.

The audit chain hash sweeper covers both audit tables; tampering with
a closed-day's flip row or a blocked-request row breaks the chain on
every subsequent day.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

SETTING_KEY = "compliance.llm_emergency_stop"

REASON_CODE = "llm-emergency-stop"

_cache_value: Optional[bool] = None
_cache_at: float = 0.0
_CACHE_TTL_SEC = 30.0


def invalidate_cache() -> None:
    global _cache_value, _cache_at
    _cache_value = None
    _cache_at = 0.0


async def _read_setting(db: AsyncSession) -> bool:
    from sqlalchemy import select
    from app.models.db import SystemSetting
    rs = await db.execute(select(SystemSetting).where(SystemSetting.key == SETTING_KEY))
    row = rs.scalar_one_or_none()
    if row is None:
        return False
    return (row.value or "").strip().lower() in ("true", "1", "yes", "on")


async def is_llm_stopped(db: AsyncSession) -> bool:
    """Hot-path check. TTL-cached so the request handler doesn't query
    SystemSetting on every call. Cache invalidates on toggle.

    Fail-OPEN on DB error (returns False). See module docstring.
    """
    global _cache_value, _cache_at
    now = time.time()
    if _cache_value is not None and (now - _cache_at) < _CACHE_TTL_SEC:
        return _cache_value
    try:
        val = await _read_setting(db)
    except Exception as exc:
        logger.warning("llm_emergency_stop.read_failed err=%r — defaulting to NOT stopped", exc)
        val = False
    _cache_value = val
    _cache_at = now
    return val


async def is_llm_stopped_session_less() -> bool:
    """Variant for background callers that don't carry a DB session
    in scope (``acompletion_with_retry`` reaches here). Opens its own
    short-lived session via the ``AsyncSessionLocal`` factory. Same
    TTL cache as ``is_llm_stopped``, so most calls don't touch the DB.
    """
    global _cache_value, _cache_at
    now = time.time()
    if _cache_value is not None and (now - _cache_at) < _CACHE_TTL_SEC:
        return _cache_value
    try:
        from app.models.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            val = await _read_setting(db)
    except Exception as exc:
        logger.warning("llm_emergency_stop.read_failed_sessionless err=%r — defaulting to NOT stopped", exc)
        val = False
    _cache_value = val
    _cache_at = now
    return val


class LLMEmergencyStopError(Exception):
    """Raised by ``acompletion_with_retry`` and the API handlers when
    the emergency stop is engaged. Carries no upstream context — by
    design, no upstream call was attempted.
    """
    def __init__(self, message: str = "LLM routing is halted by operator (emergency stop engaged)."):
        super().__init__(message)
        self.message = message


async def set_llm_stopped(
    db: AsyncSession,
    enabled: bool,
    actor: str,
    reason: Optional[str] = None,
) -> dict:
    """Flip the persistent setting + write a CompliancePolicyChange
    audit row + invalidate the cache. Returns a status dict.

    A no-op (flipping to the current state) still writes an audit row
    so the operator's intent is recorded.
    """
    from datetime import datetime
    import json as _json
    import secrets
    from sqlalchemy import select
    from app.models.db import SystemSetting, CompliancePolicyChange

    prior = await _read_setting(db)
    noop = prior == enabled
    new_state_str = "true" if enabled else "false"

    rs = await db.execute(select(SystemSetting).where(SystemSetting.key == SETTING_KEY))
    row = rs.scalar_one_or_none()
    if row is None:
        db.add(SystemSetting(
            key=SETTING_KEY, value=new_state_str,
            value_type="bool", updated_at=time.time(),
        ))
    else:
        row.value = new_state_str
        row.updated_at = time.time()

    audit_id = secrets.token_urlsafe(16)
    summary = (
        f"llm_emergency_stop {'ENGAGED' if enabled else 'DISENGAGED'} by {actor}"
        + (f"; reason: {reason}" if reason else "")
        + (" (no-op re-affirmation)" if noop else "")
    )
    db.add(CompliancePolicyChange(
        policy_change_id=audit_id,
        changed_at=datetime.utcnow(),
        changed_by_user_id=actor,
        scope="system",
        target_id=None,
        before_state=_json.dumps({"llm_emergency_stop": prior}),
        after_state=_json.dumps({"llm_emergency_stop": enabled}),
        reason=summary,
        applied_to_peers=_json.dumps([]),
        pending_peers=None,
        cluster_sync_status="local_only",
    ))

    await db.commit()
    invalidate_cache()

    logger.warning(
        "llm_emergency_stop.toggled enabled=%s actor=%s reason=%r noop=%s",
        enabled, actor, reason, noop,
    )
    return {
        "ok": True, "enabled": enabled,
        "prior_state": prior, "noop": noop,
        "audit_id": audit_id,
    }
