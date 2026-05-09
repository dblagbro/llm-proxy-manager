"""
LMRH v2 in-memory snapshot — feeds /lmrh/providers + /lmrh/health.

Background
----------

LMRH v2 (operator-approved 2026-05-09) introduces a feedback channel
where authenticated callers poll the proxy for live provider/model
metrics — cost, latency p50/p95, success rate, circuit-breaker state,
subscription quota — and use that data to construct optimal
``LLM-Hint`` headers for their next request.

To avoid a per-poll DB scan (a misbehaving caller could DoS the
ProviderMetric table), this module maintains a process-local
``Snapshot`` that's refreshed by a background task every 30 seconds.
The endpoint reads from the snapshot in O(1) per request and emits
an ETag derived from the snapshot identity, so well-behaved clients
get ``304 Not Modified`` between refreshes.

Each cluster node maintains its own snapshot (no cross-node sync of
the snapshot itself — the underlying ProviderMetric data IS already
cluster-replicated).

Public API
----------

- ``LmrhSnapshot`` — frozen dataclass with the rendered response data.
- ``get_current()`` — returns the latest snapshot (None if none built yet).
- ``start()`` — spawns the background refresh loop. Idempotent.
- ``rebuild_now(db)`` — force a fresh build (used by tests + admin).

The snapshot ships with all providers; per-key scope filtering happens
at endpoint render time so a single shared snapshot serves all callers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AsyncSessionLocal
from app.models.db import Provider, ProviderMetric, ModelCapability, ActivityLog

logger = logging.getLogger(__name__)

# Refresh cadence. Set to 30s per design doc §4.5; in-flight changes
# (CB transitions, provider edits) appear to clients within 30s. Faster
# refreshes burn DB; slower hides legitimate state shifts.
REFRESH_INTERVAL_SEC = 30

# Default time window for metric aggregation. 1h gives stable p50/p95
# while still reflecting recent regressions. Callers can request a
# different window via ``?window=24h`` (handled at endpoint render).
DEFAULT_WINDOW_SEC = 3600


@dataclass(frozen=True)
class _ModelSnap:
    model_id: str
    kind: str
    context_length: Optional[int]
    native_tools: bool
    native_reasoning: bool
    # Aggregated metrics (over DEFAULT_WINDOW_SEC ago → now). All None
    # when there's no data — distinguish "no samples" from "0 latency"
    # at the endpoint layer.
    cost_per_1m_input_usd: Optional[float]
    cost_per_1m_output_usd: Optional[float]
    rated_quota_per_1m_input_usd: Optional[float]
    latency_p50_ms: Optional[float]
    latency_p95_ms: Optional[float]
    ttft_p50_ms: Optional[float]
    ttft_p95_ms: Optional[float]
    success_rate: Optional[float]
    samples: int
    # v3.3.4: synthetic keep-alive probe stats. Surfaced separately
    # from `success_rate` so SDK callers can read both:
    #   - success_rate / samples = user-traffic only (since v3.3.3)
    #   - probe_success_rate / probe_samples = synthetic probe outcomes
    # Both come from the same window (DEFAULT_WINDOW_SEC). Probe stats
    # are cheap connectivity-health indicators ("can the proxy still
    # reach this provider?"); a probe failing while user traffic
    # succeeds is a leading indicator that real traffic may degrade
    # if the operator's auth/cookie rotation is failing. None when
    # no probes ran in the window.
    probe_success_rate: Optional[float] = None
    probe_samples: int = 0
    # v3.5.0 (LMRHv2.1) — model identity model. ``aliases`` lists
    # alternate spellings the proxy will accept (caller can send
    # either ``grok-3`` or ``x-ai/grok-3`` and route the same).
    # ``family`` is the upstream model identity (same physical model
    # regardless of provider — multiple ``_ModelSnap`` entries with
    # the same family but different ``variant`` represent multi-route
    # access to the same model). ``variant`` is the route flavour
    # ("web", "openrouter", "direct", etc.). NULL family/variant
    # means the operator hasn't classified — readers should fall
    # back to deriving family from canonical model_id.
    aliases: list[str] = field(default_factory=list)
    family: Optional[str] = None
    variant: Optional[str] = None


@dataclass(frozen=True)
class _SubQuota:
    session_used_pct: Optional[float]
    weekly_used_pct: Optional[float]
    session_resets_at: Optional[str]
    weekly_resets_at: Optional[str]


@dataclass(frozen=True)
class _ProviderSnap:
    id: str
    name: str
    type: str
    priority: int
    cost_class: str  # "subscription" | "per_call"
    circuit: str  # "open" | "closed" | "half-open"
    regions: list[str]
    # Set of api_key_ids that may route to this provider, or None if
    # unscoped (any authenticated caller). Used by endpoint render to
    # filter the response per the operator-locked scope policy
    # (decision #1: key-scoped).
    owned_by_key_id: Optional[str]
    models: list[_ModelSnap]
    subscription_quota: Optional[_SubQuota]


@dataclass(frozen=True)
class LmrhSnapshot:
    as_of: datetime
    window_sec: int
    providers: list[_ProviderSnap]
    etag: str

    def for_caller(self, key_id: str) -> list[_ProviderSnap]:
        """Filter providers per the v2 scope policy (decision #1).

        Returns providers visible to ``key_id``: any provider with
        ``owned_by_key_id is None`` (shared) plus those owned by this
        key. Operator-named providers stay private to their owners.
        """
        return [
            p for p in self.providers
            if p.owned_by_key_id is None or p.owned_by_key_id == key_id
        ]


# ── Snapshot building ───────────────────────────────────────────────────


def _percentile(sorted_vals: list[float], pct: float) -> Optional[float]:
    """Linear-interpolation percentile. ``sorted_vals`` must be sorted
    ascending. Returns None on empty input."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _circuit_state_label(state: Optional[str]) -> str:
    """Normalize circuit_state to one of {open, closed, half-open}."""
    if not state:
        return "closed"
    s = state.lower()
    if s in ("open", "closed", "half-open", "half_open"):
        return s.replace("_", "-")
    return "closed"


async def _build_snapshot(
    db: AsyncSession,
    *,
    window_sec: int = DEFAULT_WINDOW_SEC,
) -> LmrhSnapshot:
    """Build a fresh snapshot from current Provider + ModelCapability +
    ProviderMetric state. Pure read; no writes.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window_sec)

    # 1. Pull all enabled, non-deleted providers
    res = await db.execute(
        select(Provider).where(
            Provider.enabled == True,
            Provider.deleted_at.is_(None),
        ).order_by(Provider.priority.asc())
    )
    providers = list(res.scalars().all())

    # 2. Pull all ModelCapability rows for those provider ids in one query
    provider_ids = [p.id for p in providers]
    if provider_ids:
        cap_res = await db.execute(
            select(ModelCapability).where(
                ModelCapability.provider_id.in_(provider_ids),
                ModelCapability.deleted_at.is_(None),
            )
        )
        all_caps = list(cap_res.scalars().all())
    else:
        all_caps = []
    caps_by_provider: dict[str, list[ModelCapability]] = {}
    for c in all_caps:
        caps_by_provider.setdefault(c.provider_id, []).append(c)

    # 3. Pull ProviderMetric buckets in window for those providers
    metrics_by_provider: dict[str, list[ProviderMetric]] = {}
    if provider_ids:
        m_res = await db.execute(
            select(ProviderMetric).where(
                ProviderMetric.provider_id.in_(provider_ids),
                ProviderMetric.bucket_ts >= cutoff,
            )
        )
        for m in m_res.scalars().all():
            metrics_by_provider.setdefault(m.provider_id, []).append(m)

    # 3b. v3.3.4: aggregate keep-alive probe outcomes from activity_log
    # over the same window. Probes are no longer in provider_metrics
    # (excluded as of v3.3.3 to keep success_rate user-traffic-only) so
    # we count event_type='keepalive_probe' rows directly. info=success,
    # warning=failure. Cheap aggregate query: GROUP BY provider_id +
    # severity, indexed on (created_at, event_type).
    probe_stats: dict[str, dict[str, int]] = {}
    if provider_ids:
        from sqlalchemy import func as _sqlfunc
        probe_q = await db.execute(
            select(
                ActivityLog.provider_id,
                ActivityLog.severity,
                _sqlfunc.count(ActivityLog.id),
            )
            .where(
                ActivityLog.provider_id.in_(provider_ids),
                ActivityLog.event_type == "keepalive_probe",
                ActivityLog.created_at >= cutoff,
            )
            .group_by(ActivityLog.provider_id, ActivityLog.severity)
        )
        for pid, sev, cnt in probe_q.all():
            slot = probe_stats.setdefault(pid, {"successes": 0, "failures": 0})
            if sev == "info":
                slot["successes"] += int(cnt or 0)
            else:
                # warning + error both count as probe failures. Auth
                # errors raise a separate auth_failure bucket but the
                # row itself still has severity in {warning, error}.
                slot["failures"] += int(cnt or 0)

    snap_providers: list[_ProviderSnap] = []
    for p in providers:
        caps = caps_by_provider.get(p.id, [])
        metrics = metrics_by_provider.get(p.id, [])
        # Subscription cost-class derivation matches monitoring.helpers
        from app.monitoring.helpers import SUBSCRIPTION_TIER_PROVIDER_TYPES
        is_subscription = (
            p.cost_class == "subscription"
            or (p.cost_class is None and p.provider_type in SUBSCRIPTION_TIER_PROVIDER_TYPES)
        )
        cost_class = "subscription" if is_subscription else "per_call"

        # Aggregate per-model metrics. ProviderMetric is per-provider not
        # per-model, so for v3.3.0 every model on a provider shows the
        # provider's aggregate metrics. v3.3.x can split if upstream
        # reports per-model, but most cost / latency variation is at the
        # provider level (single endpoint, single billing).
        latencies = sorted(
            [float(m.avg_latency_ms) for m in metrics if m.avg_latency_ms]
        )
        ttfts = sorted(
            [float(m.avg_ttft_ms) for m in metrics
             if m.avg_ttft_ms and m.ttft_requests]
        )
        total_reqs = sum(int(m.requests or 0) for m in metrics)
        total_succ = sum(int(m.successes or 0) for m in metrics)
        total_fail = sum(int(m.failures or 0) for m in metrics)
        total_cost = sum(float(m.total_cost_usd or 0.0) for m in metrics)
        total_tokens = sum(int(m.total_tokens or 0) for m in metrics)
        # v3.4.0: per-direction cost / token aggregates. Older rows
        # (pre-v3.4.0 migration) have these at 0 — fall through to the
        # combined-rate fallback so snapshots stay populated until
        # enough new traffic accumulates.
        total_in_cost = sum(float(m.input_cost_usd or 0.0) for m in metrics)
        total_out_cost = sum(float(m.output_cost_usd or 0.0) for m in metrics)
        total_in_tok = sum(int(m.input_tokens or 0) for m in metrics)
        total_out_tok = sum(int(m.output_tokens or 0) for m in metrics)

        success_rate: Optional[float] = None
        if total_succ + total_fail > 0:
            success_rate = total_succ / (total_succ + total_fail)

        # v3.3.4: synthesise probe stats from activity_log aggregate.
        ps = probe_stats.get(p.id, {"successes": 0, "failures": 0})
        probe_succ = ps["successes"]
        probe_fail = ps["failures"]
        probe_samples = probe_succ + probe_fail
        probe_success_rate: Optional[float] = None
        if probe_samples > 0:
            probe_success_rate = probe_succ / probe_samples

        # v3.4.0: real per-direction cost rates. When the per-direction
        # token totals are non-zero (post-migration data), report
        # input + output rates independently. Fall back to the combined
        # "same rate for both" placeholder when only legacy data exists.
        cost_per_1m_input: Optional[float] = None
        cost_per_1m_output: Optional[float] = None
        if not is_subscription:
            if total_in_tok > 0:
                cost_per_1m_input = (total_in_cost / total_in_tok) * 1_000_000
            if total_out_tok > 0:
                cost_per_1m_output = (total_out_cost / total_out_tok) * 1_000_000
            # Legacy fallback: derive from combined aggregate when the
            # per-direction columns weren't populated yet.
            if cost_per_1m_input is None and total_tokens > 0:
                cost_per_1m_input = (total_cost / total_tokens) * 1_000_000
            if cost_per_1m_output is None:
                cost_per_1m_output = cost_per_1m_input

        # Rated-quota equivalent for subscription (what would cost on
        # per-call billing). For v3.3.0 we only have the aggregate
        # ``cost_usd`` — pricing.py would need the in/out split per
        # model to compute properly. Surface as None until that lands.
        rated_quota: Optional[float] = None

        p50 = _percentile(latencies, 50.0)
        p95 = _percentile(latencies, 95.0)
        ttft_p50 = _percentile(ttfts, 50.0)
        ttft_p95 = _percentile(ttfts, 95.0)

        # If no caps registered for this provider, synthesize a single
        # entry from default_model so the response isn't empty.
        if not caps and p.default_model:
            from app.api.models import _infer_kind
            from app.routing.canonical import derive_family
            model_snaps = [_ModelSnap(
                model_id=p.default_model,
                kind=_infer_kind(p.default_model),
                context_length=None,
                native_tools=False,
                native_reasoning=False,
                cost_per_1m_input_usd=cost_per_1m_input,
                cost_per_1m_output_usd=cost_per_1m_output,
                rated_quota_per_1m_input_usd=rated_quota,
                latency_p50_ms=p50,
                latency_p95_ms=p95,
                ttft_p50_ms=ttft_p50,
                ttft_p95_ms=ttft_p95,
                success_rate=success_rate,
                samples=total_reqs,
                probe_success_rate=probe_success_rate,
                probe_samples=probe_samples,
                aliases=[],
                family=derive_family(p.default_model),
                variant=None,
            )]
        else:
            from app.api.models import _infer_kind
            from app.routing.canonical import derive_family
            model_snaps = [_ModelSnap(
                model_id=c.model_id,
                kind=(c.tasks[0] if c.tasks else _infer_kind(c.model_id)),
                context_length=c.context_length,
                native_tools=bool(c.native_tools),
                native_reasoning=bool(c.native_reasoning),
                cost_per_1m_input_usd=cost_per_1m_input,
                cost_per_1m_output_usd=cost_per_1m_output,
                rated_quota_per_1m_input_usd=rated_quota,
                latency_p50_ms=p50,
                latency_p95_ms=p95,
                ttft_p50_ms=ttft_p50,
                ttft_p95_ms=ttft_p95,
                success_rate=success_rate,
                samples=total_reqs,
                probe_success_rate=probe_success_rate,
                probe_samples=probe_samples,
                aliases=list(c.aliases or []),
                family=(c.model_family or derive_family(c.model_id)),
                variant=c.model_variant,
            ) for c in caps]

        # Subscription quota — only for subscription providers; only when
        # ProviderUsageWindow has a row for this provider.
        sub_quota = None
        if is_subscription and getattr(p, "usage_tracking_enabled", False):
            try:
                from app.models.db import ProviderUsageWindow
                w_res = await db.execute(
                    select(ProviderUsageWindow).where(
                        ProviderUsageWindow.provider_id == p.id
                    )
                )
                w = w_res.scalar_one_or_none()
                if w:
                    sub_quota = _SubQuota(
                        session_used_pct=getattr(w, "session_pct", None),
                        weekly_used_pct=getattr(w, "weekly_pct", None),
                        session_resets_at=(
                            w.session_reset_at.isoformat()
                            if getattr(w, "session_reset_at", None) else None
                        ),
                        weekly_resets_at=(
                            w.weekly_reset_at.isoformat()
                            if getattr(w, "weekly_reset_at", None) else None
                        ),
                    )
            except Exception as e:
                logger.debug("usage-window lookup failed for %s: %s", p.id, e)

        # Circuit state from in-process register
        try:
            from app.routing.circuit_breaker import get_state
            cb_state_enum = await get_state(p.id)
            cb_state = cb_state_enum.value if cb_state_enum else None
        except Exception:
            cb_state = None

        # Regions: prefer first cap row's regions; fall back to []
        regions: list[str] = []
        if caps and caps[0].regions:
            regions = list(caps[0].regions)

        snap_providers.append(_ProviderSnap(
            id=p.id,
            name=p.name,
            type=p.provider_type,
            priority=p.priority,
            cost_class=cost_class,
            circuit=_circuit_state_label(cb_state),
            regions=regions,
            owned_by_key_id=p.owned_by_key_id,
            models=model_snaps,
            subscription_quota=sub_quota,
        ))

    # ETag: stable hash over identity-affecting fields. Excludes
    # ``as_of`` so equal-content snapshots produce equal ETags
    # (allows 304 between refreshes).
    digest_input = json.dumps(
        [
            {
                "id": p.id, "name": p.name, "type": p.type,
                "priority": p.priority, "cost_class": p.cost_class,
                "circuit": p.circuit, "regions": p.regions,
                "owned_by_key_id": p.owned_by_key_id,
                "models": [
                    {
                        "model_id": m.model_id, "kind": m.kind,
                        "context_length": m.context_length,
                        "native_tools": m.native_tools,
                        "native_reasoning": m.native_reasoning,
                        # round metrics so trivial float drift
                        # doesn't bust the etag every 30s
                        "latency_p50_ms": (
                            round(m.latency_p50_ms, 0) if m.latency_p50_ms else None
                        ),
                        "latency_p95_ms": (
                            round(m.latency_p95_ms, 0) if m.latency_p95_ms else None
                        ),
                        "samples": m.samples,
                        "success_rate": (
                            round(m.success_rate, 3) if m.success_rate else None
                        ),
                        # v3.3.4: include probe stats in etag input so
                        # snapshot identity reflects probe-channel state
                        # changes (a flaky upstream visible only via probes
                        # should still bust the cached etag).
                        "probe_samples": m.probe_samples,
                        "probe_success_rate": (
                            round(m.probe_success_rate, 3) if m.probe_success_rate else None
                        ),
                    }
                    for m in p.models
                ],
            }
            for p in snap_providers
        ],
        sort_keys=True, default=str,
    )
    etag = '"' + hashlib.sha1(digest_input.encode()).hexdigest()[:16] + '"'

    return LmrhSnapshot(
        as_of=now,
        window_sec=window_sec,
        providers=snap_providers,
        etag=etag,
    )


# ── Refresh loop + global accessor ─────────────────────────────────────


_current: Optional[LmrhSnapshot] = None
_task: Optional[asyncio.Task] = None
_lock = asyncio.Lock()


def get_current() -> Optional[LmrhSnapshot]:
    """Return the latest snapshot, or None if the refresh loop hasn't
    completed its first build yet. Endpoints should treat None as
    ``503 Service Unavailable`` with a short Retry-After."""
    return _current


async def rebuild_now(db: Optional[AsyncSession] = None) -> LmrhSnapshot:
    """Force a fresh build. Used at startup (so the snapshot is ready
    before the refresh loop's first tick) and by tests / admin tools.

    If ``db`` is None, opens its own session.
    """
    global _current
    if db is None:
        async with AsyncSessionLocal() as own_db:
            snap = await _build_snapshot(own_db)
    else:
        snap = await _build_snapshot(db)
    _current = snap
    return snap


async def _refresh_loop() -> None:
    """Background loop. First build is immediate; subsequent runs at
    REFRESH_INTERVAL_SEC. All errors swallowed + logged so a transient
    DB hiccup doesn't kill the loop."""
    global _current
    while True:
        try:
            await rebuild_now()
            logger.debug(
                "lmrh.snapshot.refreshed providers=%d etag=%s",
                len(_current.providers) if _current else 0,
                _current.etag if _current else None,
            )
        except Exception as e:
            logger.warning("lmrh.snapshot.refresh_failed err=%s", e)
        await asyncio.sleep(REFRESH_INTERVAL_SEC)


def start() -> None:
    """Spawn the refresh loop. Idempotent — safe to call multiple times."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_refresh_loop())
    logger.info("lmrh.snapshot.started interval_sec=%d", REFRESH_INTERVAL_SEC)
