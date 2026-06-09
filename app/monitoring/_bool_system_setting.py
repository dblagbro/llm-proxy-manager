"""v5.3.3 — shared TTL-cache + audit-on-set machinery for the two
boolean toggle modules.

Pre-refactor, ``logging_controls`` and ``llm_emergency_stop`` were
~80% identical: module-level TTL cache, ``_read_setting`` lookup
against ``system_settings``, ``is_X_enabled`` hot-path read, and
``set_X`` write-with-audit. Each new toggle would have cost ~80 LOC
of boilerplate to re-implement the same pattern. This module owns
the pattern; the two existing modules become thin shims that
construct one of these + re-export the symbols their callers grep
for.

Intentionally NOT extracted: ``retention_settings`` is multi-key
int-valued with an env-default fallback and a different cache TTL.
Folding it into this factory would add a dispatcher branch for every
method without sharing meaningful code.

Behavior preserved verbatim:
- 30s TTL cache (matched on both existing modules).
- Fail-OPEN on DB read errors (returns the configured ``default``).
  Both toggles default to fail-open today; the parameter is explicit
  so a future caller can pick fail-CLOSED if the trade-off warrants.
- Audit row written on every set, including no-ops (re-affirmations).
  Same precedent as logging_controls / llm_emergency_stop pre-refactor.
- Cache invalidates eagerly on every set so the local node honors the
  flip immediately (peers converge via 30s TTL after cluster sync).
- ``get_session_less()`` opens its own short-lived session via
  ``AsyncSessionLocal`` for callers (like ``acompletion_with_retry``)
  that don't carry a session in scope. Shares the same cache.
"""
from __future__ import annotations

import json as _json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


_TRUTHY = frozenset({"true", "1", "yes", "on"})


@dataclass
class BoolSystemSetting:
    """One cluster-replicated boolean toggle backed by ``system_settings``.

    Attributes:
        setting_key: the ``system_settings.key`` value (e.g.
            ``"compliance.activity_logging_enabled"``).
        default: value returned when the row is absent OR a DB read
            fails. ``logging_controls`` defaults True (fail-OPEN for
            observability — operator can't see logs if logging silently
            drops); ``llm_emergency_stop`` defaults False (fail-OPEN for
            routing — never halt traffic because the DB is unreachable).
        on_label: short verb pair surfaced in the audit row's ``reason``
            text. Logging uses "ENABLED"/"DISABLED"; emergency stop uses
            "ENGAGED"/"DISENGAGED". Helps operators eyeball who-did-what.
        audit_subject: prefix the audit summary uses for the action verb
            (e.g. ``"activity_logging ENABLED by alice"``).
        log_prefix: structlog/stdlib log line prefix on each toggle.
        ttl_sec: TTL cache window. 30s matched on both existing modules.
    """

    setting_key: str
    default: bool
    on_label: str
    off_label: str
    audit_subject: str
    log_prefix: str
    ttl_sec: float = 30.0

    # Mutable per-instance cache state.
    _cache_value: Optional[bool] = field(default=None, init=False, repr=False)
    _cache_at: float = field(default=0.0, init=False, repr=False)

    def invalidate_cache(self) -> None:
        self._cache_value = None
        self._cache_at = 0.0

    async def _read_from_db(self, db: AsyncSession) -> bool:
        from app.models.db import SystemSetting
        rs = await db.execute(
            select(SystemSetting).where(SystemSetting.key == self.setting_key)
        )
        row = rs.scalar_one_or_none()
        if row is None:
            return self.default
        return (row.value or "").strip().lower() in _TRUTHY

    async def get(self, db: AsyncSession) -> bool:
        """Hot-path read used by the request handlers. TTL-cached.

        Fail-OPEN: on DB error the configured ``default`` is returned.
        See class docstring for the rationale on each existing module.
        """
        now = time.time()
        if self._cache_value is not None and (now - self._cache_at) < self.ttl_sec:
            return self._cache_value
        try:
            val = await self._read_from_db(db)
        except Exception as exc:
            logger.warning(
                "%s.read_failed key=%s err=%r — defaulting to %s",
                self.log_prefix, self.setting_key, exc, self.default,
            )
            val = self.default
        self._cache_value = val
        self._cache_at = now
        return val

    async def get_session_less(self) -> bool:
        """Variant for callers that don't carry a DB session in scope.
        Opens a short-lived session via ``AsyncSessionLocal`` only on
        cache miss; the TTL cache absorbs the steady-state cost so
        background hot paths don't pay for the session per call."""
        now = time.time()
        if self._cache_value is not None and (now - self._cache_at) < self.ttl_sec:
            return self._cache_value
        try:
            from app.models.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                val = await self._read_from_db(db)
        except Exception as exc:
            logger.warning(
                "%s.read_failed_sessionless key=%s err=%r — defaulting to %s",
                self.log_prefix, self.setting_key, exc, self.default,
            )
            val = self.default
        self._cache_value = val
        self._cache_at = now
        return val

    async def set(
        self,
        db: AsyncSession,
        enabled: bool,
        actor: str,
        reason: Optional[str] = None,
    ) -> dict:
        """Persist + audit + invalidate. Returns a status dict matching
        the pre-refactor contract of both modules (``ok``, ``enabled``,
        ``prior_state``, ``noop``, ``audit_id``).

        A no-op (flipping to the current state) still writes an audit
        row so the operator's intent is recorded. Matches both modules'
        pre-refactor behavior.
        """
        from app.models.db import SystemSetting, CompliancePolicyChange

        prior = await self._read_from_db(db)
        noop = prior == enabled
        new_state_str = "true" if enabled else "false"

        rs = await db.execute(
            select(SystemSetting).where(SystemSetting.key == self.setting_key)
        )
        row = rs.scalar_one_or_none()
        if row is None:
            db.add(SystemSetting(
                key=self.setting_key,
                value=new_state_str,
                value_type="bool",
                updated_at=time.time(),
            ))
        else:
            row.value = new_state_str
            row.updated_at = time.time()

        audit_id = secrets.token_urlsafe(16)
        verb = self.on_label if enabled else self.off_label
        summary = (
            f"{self.audit_subject} {verb} by {actor}"
            + (f"; reason: {reason}" if reason else "")
            + (" (no-op re-affirmation)" if noop else "")
        )
        db.add(CompliancePolicyChange(
            policy_change_id=audit_id,
            changed_at=datetime.utcnow(),
            changed_by_user_id=actor,
            scope="system",
            target_id=None,
            before_state=_json.dumps({self.audit_subject: prior}),
            after_state=_json.dumps({self.audit_subject: enabled}),
            reason=summary,
            applied_to_peers=_json.dumps([]),
            pending_peers=None,
            cluster_sync_status="local_only",
        ))

        await db.commit()
        self.invalidate_cache()

        logger.warning(
            "%s enabled=%s actor=%s reason=%r noop=%s",
            self.log_prefix, enabled, actor, reason, noop,
        )
        return {
            "ok": True,
            "enabled": enabled,
            "prior_state": prior,
            "noop": noop,
            "audit_id": audit_id,
        }
