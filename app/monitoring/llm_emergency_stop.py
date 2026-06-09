"""v5.2.0 / Batch V1 — LLM-call emergency stop (kill switch).

v5.3.3 — refactored to a thin shim over ``_bool_system_setting``. The
TTL cache + ``_read_setting`` + audit-on-set + session-less variant
machinery now lives in the shared factory; this module pins the
parameters that make THIS toggle different (default OFF, "ENGAGED/
DISENGAGED" wording, reason_code).

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
  - TTL cache 30s. Flips invalidate eagerly on the toggle endpoint;
    peers converge via TTL.
  - Fail-OPEN on DB error. An operator who can't see whether the kill
    switch is ON has a worse mode (silent traffic drop) than one who
    sees their kill switch ineffectual.
  - Audit row per flip: ``compliance_policy_changes`` scope='system'.
  - Audit row per blocked request: ``compliance_events`` with
    ``reason_code='llm-emergency-stop'``.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.monitoring._bool_system_setting import BoolSystemSetting

SETTING_KEY = "compliance.llm_emergency_stop"

REASON_CODE = "llm-emergency-stop"


_setting = BoolSystemSetting(
    setting_key=SETTING_KEY,
    default=False,  # fail-OPEN — never halt traffic on DB read error
    on_label="ENGAGED",
    off_label="DISENGAGED",
    audit_subject="llm_emergency_stop",
    log_prefix="llm_emergency_stop.toggled",
    ttl_sec=30.0,
)


def invalidate_cache() -> None:
    _setting.invalidate_cache()


async def is_llm_stopped(db: AsyncSession) -> bool:
    """Hot-path check. TTL-cached so the request handler doesn't query
    SystemSetting on every call. Cache invalidates on toggle.

    Fail-OPEN on DB error (returns False)."""
    return await _setting.get(db)


async def is_llm_stopped_session_less() -> bool:
    """Variant for background callers that don't carry a DB session
    in scope (``acompletion_with_retry`` reaches here). Opens its own
    short-lived session via the ``AsyncSessionLocal`` factory. Same
    TTL cache as ``is_llm_stopped``, so most calls don't touch the DB.
    """
    return await _setting.get_session_less()


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
    return await _setting.set(db, enabled, actor, reason)
