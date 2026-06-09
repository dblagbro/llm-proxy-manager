"""v5.1.0 / Batch C1 — Activity-log on/off toggle (compliance panic button).

v5.3.3 — refactored to a thin shim over ``_bool_system_setting``. The
TTL cache + ``_read_setting`` + audit-on-set machinery now lives in the
shared factory; this module pins the parameters that make THIS toggle
different (default ON, "ENABLED/DISABLED" wording, fail-OPEN rationale).

Pre-refactor: when proxy traffic carries PII, the operator needs to be
able to halt activity-log capture without redeploying.

  - ``is_logging_enabled(db)`` — module-cached read of the
    ``compliance.activity_logging_enabled`` system_setting. Default ON
    (pre-feature behavior preserved — fail-OPEN: an operator who can't
    see logs has no signal that something is broken).
  - ``set_logging_enabled(db, enabled, actor, reason)`` — flips the
    persistent setting AND writes an audit row to
    ``compliance_policy_changes``. Invalidates the in-process cache so
    the change takes effect on the next ``log_event`` call.
  - ``invalidate_cache()`` — re-reads from DB on next check.

Audit rows themselves NEVER purgeable. The ``compliance_policy_changes``
table is the operator's proof of who flipped logging and when.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.monitoring._bool_system_setting import BoolSystemSetting

# system_settings key. Public name preserved for test source-pins
# and admin-endpoint use.
SETTING_KEY = "compliance.activity_logging_enabled"


_setting = BoolSystemSetting(
    setting_key=SETTING_KEY,
    default=True,  # fail-OPEN — see module docstring
    on_label="ENABLED",
    off_label="DISABLED",
    audit_subject="activity_logging",
    log_prefix="logging_controls.toggled",
    ttl_sec=30.0,
)


def invalidate_cache() -> None:
    _setting.invalidate_cache()


async def is_logging_enabled(db: AsyncSession) -> bool:
    """Cheap async check used by ``log_event``. TTL-cached to avoid
    hitting the DB on every event. ``invalidate_cache()`` from the
    toggle endpoint clears the cache so flips take effect at once."""
    return await _setting.get(db)


async def set_logging_enabled(
    db: AsyncSession,
    enabled: bool,
    actor: str,
    reason: Optional[str] = None,
) -> dict:
    """Flip the persistent setting + write an audit row + invalidate
    the cache. Returns a small status dict."""
    return await _setting.set(db, enabled, actor, reason)
