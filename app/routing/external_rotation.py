"""v3.7.1 — auto-rotation rules driven by external usage snapshots.

The v3.7.0 scraper now writes authoritative weekly utilization to
``ExternalUsageSnapshot``. This module consumes those snapshots and
turns them into routing decisions:

  Rule 1 (capacity skip):
    If latest snapshot shows ``seven_day_utilization >= threshold``
    (default 95%, configurable via ``external_rotation_capacity_pct``),
    set ``Provider.auto_skip_until = seven_day_resets_at``. The
    router filters out providers whose ``auto_skip_until`` is in the
    future, so traffic naturally rotates away from at-capacity
    providers and back to them after the reset timestamp passes.

  Rule 2 (clear when below threshold):
    If utilization is back below ``threshold - hysteresis_pct``
    (default 90%), clear ``auto_skip_until``. The hysteresis avoids
    flapping when utilization sits exactly at the threshold.

The router itself (``app/routing/router.py:select_provider``) skips
on the timestamp comparison — so a provider's ``auto_skip_until`` in
the past is effectively cleared even before the next scrape. The
explicit clear in Rule 2 keeps the DB clean and the admin UI honest.

This module is INTENTIONALLY simple. v3.7.x can extend it (per-model
breakdowns from ``seven_day_sonnet_utilization``, 5-hour bursts via
``five_hour_utilization``, etc.) once we've validated the basic
weekly-cap behavior in production.

Operator-set ``Provider.priority`` and ``Provider.enabled`` are
preserved unchanged — auto-rotation is additive.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ExternalUsageSnapshot, Provider

logger = logging.getLogger(__name__)

# Defaults applied when no settings are present. Operator can override
# via env (``EXTERNAL_ROTATION_CAPACITY_PCT`` etc).
_DEFAULT_CAPACITY_PCT = 95.0
_DEFAULT_HYSTERESIS_PCT = 5.0


def _capacity_threshold() -> float:
    try:
        from app.config import settings
        v = getattr(settings, "external_rotation_capacity_pct", None)
        if v is not None:
            return float(v)
    except Exception:
        pass
    return _DEFAULT_CAPACITY_PCT


def _hysteresis_pct() -> float:
    try:
        from app.config import settings
        v = getattr(settings, "external_rotation_hysteresis_pct", None)
        if v is not None:
            return float(v)
    except Exception:
        pass
    return _DEFAULT_HYSTERESIS_PCT


async def _latest_snapshot(db: AsyncSession, provider_id: str) -> Optional[ExternalUsageSnapshot]:
    rs = await db.execute(
        select(ExternalUsageSnapshot)
        .where(ExternalUsageSnapshot.provider_id == provider_id)
        .order_by(desc(ExternalUsageSnapshot.captured_at))
        .limit(1)
    )
    return rs.scalar_one_or_none()


async def evaluate_rules_for_provider(
    db: AsyncSession,
    provider: Provider,
    *,
    snapshot: Optional[ExternalUsageSnapshot] = None,
) -> dict:
    """Apply auto-rotation rules to a single provider.

    If ``snapshot`` is passed, evaluate against it directly (caller
    just wrote it via ``scrape_provider_into_snapshot``). Otherwise
    fetch the most recent snapshot for the provider.

    Returns a small dict describing the decision so the caller can
    log it (or surface it in the admin endpoint response).

    The DB write is committed by the caller — we only mutate the
    ``provider`` instance. This lets the scraper's outer commit batch
    the snapshot insert + the provider update.
    """
    snap = snapshot if snapshot is not None else await _latest_snapshot(db, provider.id)
    if snap is None:
        return {
            "provider_id": provider.id,
            "decision": "no_snapshot",
            "auto_skip_until": _iso(provider.auto_skip_until),
        }
    util = snap.seven_day_utilization
    if util is None:
        return {
            "provider_id": provider.id,
            "decision": "no_utilization",
            "auto_skip_until": _iso(provider.auto_skip_until),
        }
    threshold = _capacity_threshold()
    clear_below = max(0.0, threshold - _hysteresis_pct())
    prior_skip = provider.auto_skip_until
    prior_reason = provider.auto_skip_reason
    decision: str
    new_skip_until: Optional[datetime] = prior_skip
    new_reason: Optional[str] = prior_reason

    if util >= threshold:
        new_skip_until = snap.seven_day_resets_at
        new_reason = (
            f"weekly utilization {util:.1f}% >= {threshold:.0f}% threshold; "
            f"resets {_iso(snap.seven_day_resets_at)}"
        )
        decision = "skip_set"
    elif util <= clear_below and prior_skip is not None:
        new_skip_until = None
        new_reason = None
        decision = "skip_cleared"
    else:
        # In the hysteresis band → don't change state, just report
        decision = "no_change"

    if new_skip_until != prior_skip or new_reason != prior_reason:
        provider.auto_skip_until = new_skip_until
        provider.auto_skip_reason = new_reason

    out = {
        "provider_id": provider.id,
        "provider_name": provider.name,
        "seven_day_utilization": util,
        "threshold_pct": threshold,
        "decision": decision,
        "auto_skip_until": _iso(new_skip_until),
        "auto_skip_reason": new_reason,
        "prior_auto_skip_until": _iso(prior_skip),
    }
    if decision != "no_change":
        logger.info("external_rotation.decision %s", out)
    return out


async def evaluate_rules_for_all_providers(db: AsyncSession) -> list[dict]:
    """Convenience: evaluate every claude-oauth provider that has a
    snapshot. Used by the manual-trigger admin endpoint and as a
    one-shot from the scraper worker after a sweep."""
    rs = await db.execute(
        select(Provider)
        .where(Provider.provider_type == "claude-oauth")
        .where(Provider.deleted_at.is_(None))
    )
    out = []
    any_changed = False
    for p in rs.scalars().all():
        decision = await evaluate_rules_for_provider(db, p)
        out.append(decision)
        if decision.get("decision") in ("skip_set", "skip_cleared"):
            any_changed = True
    if any_changed:
        await db.commit()
    return out


def is_currently_at_capacity(provider: Provider) -> bool:
    """Routing-time check: true iff this provider should be skipped
    right now because of an active auto-rotation skip window.

    A timestamp in the past is treated as cleared even if the rule
    evaluator hasn't run yet — the next scrape will tidy up.
    """
    skip = provider.auto_skip_until
    if skip is None:
        return False
    now = datetime.now(timezone.utc)
    # Tolerate naive datetimes from SQLite (no tz), assume UTC.
    if skip.tzinfo is None:
        skip = skip.replace(tzinfo=timezone.utc)
    return skip > now


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()
