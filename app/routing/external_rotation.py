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

v5.0.15 — also clamp on the session bucket. Anthropic's billing API
exposes ``five_hour_utilization`` (resets every 5h). Pre-v5.0.15 the
rotation logic ignored it; a provider at 100% session / 13% weekly
kept getting picked because weekly looked healthy. Now a provider
is skipped if EITHER bucket exhausts (weekly with hysteresis, session
as a hard 100% cap) and ``auto_skip_until`` is the LATER of the two
reset times so we don't release while one bucket is still capped.

Per-model breakdowns from ``seven_day_sonnet_utilization`` etc. are
still deferred — same pattern, no concrete trigger yet.

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
    util_weekly = snap.seven_day_utilization
    # v5.0.15 — Anthropic's billing API exposes a SEPARATE session-window
    # utilization (``five_hour``) that hits 100% on session-max well
    # before the weekly cap. Pre-v5.0.15 the rotation logic only looked
    # at ``seven_day_utilization`` — so when a provider's session window
    # exhausted (e.g. Devin-Anthropic-Max-VG at 100% five-hour / 13%
    # weekly on 2026-06-04) the router kept picking it as if it were
    # healthy. Now we treat the session bucket as a hard 100% cap (no
    # hysteresis — it's not a tunable policy, it's an upstream lockout)
    # and the weekly bucket keeps its existing soft threshold +
    # hysteresis. The provider is skipped if EITHER bucket exhausts;
    # ``auto_skip_until`` is the LATER of the two reset times so we
    # don't release prematurely while one bucket is still capped.
    util_session = snap.five_hour_utilization
    if util_weekly is None and util_session is None:
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

    session_exhausted = util_session is not None and util_session >= 100.0
    weekly_exhausted = util_weekly is not None and util_weekly >= threshold

    if session_exhausted or weekly_exhausted:
        # Collect the active resets so skip_until covers ALL exhausted
        # buckets — if both are capped, the LATER reset wins.
        candidates: list[datetime] = []
        parts: list[str] = []
        if session_exhausted:
            if snap.five_hour_resets_at is not None:
                candidates.append(snap.five_hour_resets_at)
            parts.append(
                f"session utilization {util_session:.1f}% >= 100%; "
                f"resets {_iso(snap.five_hour_resets_at)}"
            )
        if weekly_exhausted:
            if snap.seven_day_resets_at is not None:
                candidates.append(snap.seven_day_resets_at)
            parts.append(
                f"weekly utilization {util_weekly:.1f}% "
                f">= {threshold:.0f}% threshold; "
                f"resets {_iso(snap.seven_day_resets_at)}"
            )
        new_skip_until = max(candidates) if candidates else None
        new_reason = "; ".join(parts)
        decision = "skip_set"
    elif prior_skip is not None:
        # Clear ONLY when BOTH buckets are confirmed healthy: weekly
        # below the hysteresis floor AND session below 100%. A missing
        # value (None) is treated as healthy so we don't strand a
        # skip on a provider whose scraper temporarily lost a window.
        weekly_ok = (util_weekly is None) or (util_weekly <= clear_below)
        session_ok = (util_session is None) or (util_session < 100.0)
        if weekly_ok and session_ok:
            new_skip_until = None
            new_reason = None
            decision = "skip_cleared"
        else:
            decision = "no_change"
    else:
        decision = "no_change"

    if new_skip_until != prior_skip or new_reason != prior_reason:
        provider.auto_skip_until = new_skip_until
        provider.auto_skip_reason = new_reason

    out = {
        "provider_id": provider.id,
        "provider_name": provider.name,
        "seven_day_utilization": util_weekly,
        "five_hour_utilization": util_session,    # v5.0.15
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
    """Convenience: evaluate every snapshot-bearing provider type
    (claude-oauth + cursor-oauth as of v4.4.41) that has a snapshot.
    Used by the manual-trigger admin endpoint and as a one-shot from
    the scraper workers after a sweep."""
    rs = await db.execute(
        select(Provider)
        .where(Provider.provider_type.in_(("claude-oauth", "cursor-oauth")))
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


# v3.7.4 — utilization-weighted preference for claude-oauth providers.
# In-process cache keyed by node. Refreshes when the underlying
# snapshot data changes (we don't watch for that — just a 30s TTL).
import time as _time
_util_cache: dict[str, tuple[float, dict[str, float]]] = {}
_UTIL_CACHE_TTL_SEC = 30.0


async def get_utilization_map(db: AsyncSession) -> dict[str, float]:
    """Return {provider_id: latest seven_day_utilization} for every
    provider with a non-null snapshot. Cached 30s per process.

    Used by the router to re-order claude-oauth providers by current
    headroom. Returns an empty dict if the snapshot table is empty
    (graceful degrade — router falls back to operator priority).
    """
    now = _time.monotonic()
    cached = _util_cache.get("default")
    if cached and cached[0] > now:
        return cached[1]
    rs = await db.execute(
        select(
            ExternalUsageSnapshot.provider_id,
            ExternalUsageSnapshot.seven_day_utilization,
            ExternalUsageSnapshot.captured_at,
        )
        .where(ExternalUsageSnapshot.seven_day_utilization.is_not(None))
        .order_by(desc(ExternalUsageSnapshot.captured_at))
    )
    # Walk results newest-first, take first non-null per provider.
    util_map: dict[str, float] = {}
    for pid, util, _ in rs.all():
        if pid not in util_map:
            util_map[pid] = float(util)
    _util_cache["default"] = (now + _UTIL_CACHE_TTL_SEC, util_map)
    return util_map


def _utilization_bucket(util: Optional[float], bucket_size_pct: float = 25.0) -> int:
    """Convert a utilization percent into a coarse bucket index.

    Returns a non-negative integer; smaller = more headroom = preferred.
    Default bucket size 25pp → 0=0-24%, 1=25-49%, 2=50-74%, 3=75-99%, 4=100%.
    A coarse-grained bucket avoids constant reordering when utilization
    changes by trivial amounts. ``None`` (no snapshot) maps to the
    "no data" bucket which sorts AFTER known-good buckets, so a
    provider with snapshot data preferentially wins over one without.
    """
    if util is None:
        return 999  # "no data" — sorts last among claude-oauth
    if util < 0:
        return 0
    return max(0, int(util // max(bucket_size_pct, 1.0)))


def reorder_subscription_by_utilization(
    providers: list[Provider],
    util_map: dict[str, float],
    *,
    provider_type: str,
    bucket_size_pct: float = 25.0,
) -> list[Provider]:
    """v4.4.41 — generalization of the original ``claude-oauth`` reorder.
    Re-sorts providers of a single ``provider_type`` by
    (utilization_bucket, operator_priority); leaves all other entries
    in their original positions. Called once per subscription-tier type
    that has a usage-monitoring scrape (today: claude-oauth via the
    Anthropic Console scrape; cursor-oauth via the Cursor dashboard
    scrape). No-op when fewer than 2 providers of ``provider_type`` are
    present.
    """
    if not providers:
        return providers
    oauth_positions = [i for i, p in enumerate(providers) if p.provider_type == provider_type]
    if len(oauth_positions) < 2:
        return providers
    oauth_subset = [providers[i] for i in oauth_positions]

    def _key(p: Provider) -> tuple:
        util = util_map.get(p.id)
        bucket = _utilization_bucket(util, bucket_size_pct)
        return (bucket, p.priority)

    sorted_subset = sorted(oauth_subset, key=_key)
    if all(a is b for a, b in zip(sorted_subset, oauth_subset)):
        return providers
    result = list(providers)
    for idx, p in zip(oauth_positions, sorted_subset):
        result[idx] = p
    return result


def reorder_claude_oauth_by_utilization(
    providers: list[Provider],
    util_map: dict[str, float],
    *,
    bucket_size_pct: float = 25.0,
) -> list[Provider]:
    """v3.7.4 — claude-oauth + (v4.4.41) cursor-oauth multi-vendor
    preferred-pick: route through the subscription account with more
    headroom inside each vendor's account pool. Each subscription
    type's reorder is scoped to its own subset (claude accounts don't
    reorder cursor positions and vice versa).

    Operator-set priority encodes cost class, account preference, and
    other dimensions not captured by utilization. We don't want to
    route a $$$ per-call provider just because its utilization happens
    to be lower than a subscription provider. Limiting the reorder to
    within each subscription subset preserves the operator's
    coarse-grained ordering while expressing "use the account with
    more headroom" within the subscription pool.

    Function name kept for back-compat with the router callsite; the
    new public name is ``reorder_subscription_by_utilization``.
    """
    out = reorder_subscription_by_utilization(
        providers, util_map,
        provider_type="claude-oauth", bucket_size_pct=bucket_size_pct,
    )
    out = reorder_subscription_by_utilization(
        out, util_map,
        provider_type="cursor-oauth", bucket_size_pct=bucket_size_pct,
    )
    return out


def _bucket_size_setting() -> float:
    try:
        from app.config import settings
        v = getattr(settings, "external_rotation_util_bucket_pct", None)
        if v is not None:
            return float(v)
    except Exception:
        pass
    return 25.0


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()
