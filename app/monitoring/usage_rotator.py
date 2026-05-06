"""Auto-rotate provider priorities based on usage gap (v3.0.65, Phase 3).

Reads ``provider_usage_windows`` populated by ``usage_tracker``. For each
group of providers sharing a ``provider_type`` (e.g. all claude-oauth) where
both are tracking-enabled and have rotation thresholds set, computes the
hotter window's used-pct (max of session_pct, weekly_pct), and if the gap
between the most-used and least-used exceeds the configured
``usage_rotation_threshold_pct``, swaps their priorities so the under-used
provider becomes priority 1.

Defaults locked from operator's design questions (chat 2026-05-05):
  - Rotation primitive: hard swap of priority (matches manual workflow)
  - Pair definition: same provider_type only
  - Threshold semantics: gap (max_pct - min_pct), in percentage points
  - Window: hotter (max of session, weekly) drives the comparison
  - Cooldown: 30 min between rotations on the same pair
  - NULL safety: skip if either side has NULL usage_session_limit_tokens
    AND NULL usage_weekly_limit_tokens (no pct can be computed)

Logged via activity_log so the operator can audit rotations + revert.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from sqlalchemy import select

from app.config import settings
from app.models.database import AsyncSessionLocal
from app.models.db import Provider, ProviderUsageWindow
from app.monitoring.activity import log_event

logger = logging.getLogger(__name__)


_INTERVAL_SEC_DEFAULT = 300       # check every 5 min
_COOLDOWN_SEC_DEFAULT = 1800      # 30 min between rotations on same pair


def _interval_sec() -> int:
    try:
        v = int(getattr(settings, "usage_rotator_interval_sec", _INTERVAL_SEC_DEFAULT))
        return max(60, v)
    except Exception:
        return _INTERVAL_SEC_DEFAULT


def _cooldown_sec() -> int:
    try:
        v = int(getattr(settings, "usage_rotator_cooldown_sec", _COOLDOWN_SEC_DEFAULT))
        return max(0, v)
    except Exception:
        return _COOLDOWN_SEC_DEFAULT


# pair_key (sorted tuple of provider ids) -> last rotation unix ts.
_LAST_ROTATION: dict[tuple[str, str], float] = {}


def _hotter_pct(w: ProviderUsageWindow) -> Optional[float]:
    """Returns max(session_pct, weekly_pct) ignoring None. None if both null."""
    pcts = [v for v in (w.session_pct, w.weekly_pct) if v is not None]
    return max(pcts) if pcts else None


async def _rotate_one_group(provider_type: str) -> int:
    """Walk one provider_type group; rotate if gap exceeds threshold.
    Returns 1 if a rotation happened, 0 otherwise."""
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(Provider).where(
                Provider.provider_type == provider_type,
                Provider.enabled == True,  # noqa: E712
                Provider.deleted_at.is_(None),
                Provider.usage_tracking_enabled == True,  # noqa: E712
            ).order_by(Provider.priority)
        )
        providers = list(res.scalars().all())
        if len(providers) < 2:
            return 0

        windows_res = await db.execute(
            select(ProviderUsageWindow).where(
                ProviderUsageWindow.provider_id.in_([p.id for p in providers]),
            )
        )
        windows = {w.provider_id: w for w in windows_res.scalars().all()}

        # Compute hotter pct per provider; require both pcts available
        with_pct = []
        for p in providers:
            w = windows.get(p.id)
            if w is None:
                continue
            pct = _hotter_pct(w)
            if pct is None:
                continue
            with_pct.append((p, pct))

        if len(with_pct) < 2:
            return 0

        with_pct.sort(key=lambda t: t[1])  # ascending pct
        low_p, low_pct = with_pct[0]
        high_p, high_pct = with_pct[-1]

        # Threshold: use the MAX threshold across both providers' configs.
        # (Operator may set per-provider; pick the more permissive value so
        # neither side rotates more aggressively than its own setting.)
        thresholds = [p.usage_rotation_threshold_pct for p in providers
                      if p.usage_rotation_threshold_pct is not None]
        if not thresholds:
            return 0
        threshold_pct = max(thresholds)

        gap = high_pct - low_pct
        if gap <= threshold_pct:
            return 0

        # Already in correct order? (least-used has lower priority number)
        if low_p.priority < high_p.priority:
            return 0

        # Cooldown check
        pair_key = tuple(sorted([low_p.id, high_p.id]))
        now = time.time()
        last = _LAST_ROTATION.get(pair_key, 0)
        if now - last < _cooldown_sec():
            return 0

        # Do the swap. Bump last_user_edit_at so cluster-sync LWW preserves
        # the rotation across nodes (v3.0.63 strict-greater).
        old_low_pri, old_high_pri = low_p.priority, high_p.priority
        low_p.priority = old_high_pri
        high_p.priority = old_low_pri
        low_p.last_user_edit_at = now
        high_p.last_user_edit_at = now + 0.001  # break ties deterministically
        await db.commit()
        _LAST_ROTATION[pair_key] = now

        # Log the rotation
        try:
            async with AsyncSessionLocal() as db2:
                await log_event(
                    db2,
                    event_type="usage_rotation",
                    severity="info",
                    message=(
                        f"usage-rotation [{provider_type}] {high_p.name} "
                        f"({high_pct:.1f}%) → priority {old_low_pri}; "
                        f"{low_p.name} ({low_pct:.1f}%) → priority {old_high_pri}; "
                        f"gap={gap:.1f}% threshold={threshold_pct}%"
                    ),
                    metadata={
                        "provider_type": provider_type,
                        "rotated_to_top": low_p.id,
                        "rotated_to_top_name": low_p.name,
                        "rotated_to_top_pct": low_pct,
                        "rotated_to_top_old_priority": old_low_pri,
                        "rotated_to_bottom": high_p.id,
                        "rotated_to_bottom_name": high_p.name,
                        "rotated_to_bottom_pct": high_pct,
                        "rotated_to_bottom_old_priority": old_high_pri,
                        "gap_pct": gap,
                        "threshold_pct": threshold_pct,
                    },
                )
        except Exception as e:
            logger.warning("usage_rotator.log_failed err=%s", e)
        return 1


async def _sweep_once() -> int:
    """Walk every distinct provider_type that has tracking-enabled rows.
    Returns count of rotations performed."""
    async with AsyncSessionLocal() as db:
        types_res = await db.execute(
            select(Provider.provider_type).where(
                Provider.enabled == True,  # noqa: E712
                Provider.deleted_at.is_(None),
                Provider.usage_tracking_enabled == True,  # noqa: E712
            ).distinct()
        )
        types = [r[0] for r in types_res.all()]

    rotations = 0
    for pt in types:
        try:
            rotations += await _rotate_one_group(pt)
        except Exception as e:
            logger.warning("usage_rotator.group_failed type=%s err=%s", pt, e)
    return rotations


async def _loop() -> None:
    """Periodic loop. Boot-delayed 90s so usage_tracker has populated cache."""
    await asyncio.sleep(90)
    while True:
        interval = _interval_sec()
        try:
            n = await _sweep_once()
            if n:
                logger.info("usage_rotator.swept rotations=%d", n)
        except Exception as e:
            logger.warning("usage_rotator.sweep_failed err=%s", e)
        await asyncio.sleep(interval)


_TASK: Optional[asyncio.Task] = None


def start() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop(), name="usage-rotator-loop")
