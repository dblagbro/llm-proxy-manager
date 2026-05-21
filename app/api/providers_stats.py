"""v4.4.14 — provider read-side / stats endpoints extracted from
``app/api/providers.py`` to keep that file under the 1000-LOC ceiling.

Endpoints:
- GET /api/providers                          — list (with usage merge)
- GET /api/providers/rolling-stats            — per-provider rolling windows
- GET /api/providers/rolling-stats-by-node    — per-(provider, node) breakout
- GET /api/providers/{id}/usage               — per-provider usage snapshot

Mirrors the v3.9.8 P5 pattern already used by
``provider_lifecycle.py`` and ``provider_capabilities.py``: each
sibling owns its own ``APIRouter`` at the ``/api/providers``
prefix; ``main.py`` includes them separately. Endpoint paths don't
overlap, so FastAPI's routing happily dispatches.

Shared helper ``_serialize`` is imported from ``providers.py``
(canonical home for provider→dict serialization).
"""
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.models.db import (
    Provider, ExternalUsageSnapshot, ProviderUsageWindow, ActivityLog,
)
from app.auth.admin import require_admin, AdminUser
from app.utils.timefmt import utc_iso


router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("")
async def list_providers(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    # Avoid the import cycle: providers.py imports providers_stats
    # at the end of its module (after _serialize is defined), and
    # this lazy import is the only safe way to reach the canonical
    # _serialize without circular-import errors at module load.
    from app.api.providers import _serialize

    # v2.8.2: hide soft-deleted (tombstoned) providers from the UI.
    result = await db.execute(
        select(Provider)
        .where(Provider.deleted_at.is_(None))
        .order_by(Provider.priority)
    )
    providers = result.scalars().all()
    # v3.9.8 (#267 follow-up) — Authoritative usage data from
    # ExternalUsageSnapshot (Anthropic Console + ChatGPT Cloud scrape,
    # shipped v3.7.0 + v3.8.1) supersedes ProviderUsageWindow for
    # display. Background: ProviderUsageWindow only sees the PROXY's
    # slice of traffic to each upstream account. The same Anthropic Pro
    # Max / ChatGPT Plus accounts are shared with Claude Code / desktop
    # clients / other workloads, so the proxy slice is ~3 orders of
    # magnitude lower than the account total. Pre-fix, this produced
    # nonsense "weekly 643%" warnings on the Dashboard because the
    # operator-set ``usage_weekly_limit_tokens`` was sized for the
    # proxy slice but the rolled-up counter was the account total.
    #
    # The scrape stores authoritative 0-100% utilization directly from
    # Anthropic's / ChatGPT's own metering. Use that when present; fall
    # back to ProviderUsageWindow only when no snapshot exists (e.g.
    # newly-added providers, providers without a captured session).

    # Latest snapshot per provider (one query, ordered by captured_at DESC,
    # then dedup in Python — small fleet so this is cheap).
    snap_res = await db.execute(
        select(ExternalUsageSnapshot)
        .order_by(desc(ExternalUsageSnapshot.captured_at))
    )
    snap_by_provider: dict[str, ExternalUsageSnapshot] = {}
    for snap in snap_res.scalars().all():
        snap_by_provider.setdefault(snap.provider_id, snap)

    # Internal usage windows (fallback for non-scraped providers)
    usage_res = await db.execute(select(ProviderUsageWindow))
    usage_by_id = {w.provider_id: w for w in usage_res.scalars().all()}

    out = []
    for p in providers:
        d = _serialize(p)
        snap = snap_by_provider.get(p.id)
        w = usage_by_id.get(p.id)
        if snap is not None and snap.seven_day_utilization is not None:
            # Authoritative path — scrape data wins.
            d["usage_weekly_pct"] = snap.seven_day_utilization
            d["usage_session_pct"] = snap.five_hour_utilization
            d["usage_weekly_tokens"] = None  # not available from scrape
            d["usage_session_tokens"] = None
            d["usage_data_source"] = "external_scrape"
            d["usage_captured_at"] = (
                snap.captured_at.isoformat() if snap.captured_at else None
            )
        elif w is not None:
            d["usage_session_pct"] = w.session_pct
            d["usage_session_tokens"] = w.session_tokens
            d["usage_weekly_pct"] = w.weekly_pct
            d["usage_weekly_tokens"] = w.weekly_tokens
            d["usage_data_source"] = "internal_window"
        out.append(d)
    return out


@router.get("/rolling-stats")
async def provider_rolling_stats(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.0.39: per-provider request volume + success rate across rolling
    1h / 24h / 7d / 30d windows. Backs the new columns on the provider list
    page (operator ask 2026-05-01).

    Returns: list of {provider_id, provider_name, windows: {1h, 24h, 7d, 30d}}
    where each window has {requests, successes, success_pct}. Providers with
    no traffic in the 30d window are omitted; the frontend treats absence as
    'no data'.
    """
    from app.monitoring.metrics import get_provider_rolling_windows
    return await get_provider_rolling_windows(db)


@router.get("/rolling-stats-by-node")
async def provider_rolling_stats_by_node(
    window_hours: int = 24,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.9.16 (P3b) — per-(provider × node) rollup over a rolling window.

    Backs the new Provider Summary "Per-node" toggle. Per-node breakouts
    surface unbalanced load and let operators decide if they should route
    more or less traffic to a specific node.

    Reads from ``activity_log.event_meta.node_id`` (added in v3.9.16).
    Pre-v3.9.16 rows have null node_id; they roll into a synthetic
    ``"unknown"`` bucket the UI can display as legacy traffic.

    Response shape:
      [
        {
          "provider_id": str,
          "provider_name": str,
          "by_node": {
            "<node_id>": {"requests": int, "successes": int, "success_pct": float},
            ...
          }
        },
        ...
      ]
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        hours=max(1, min(window_hours, 720)),
    )
    # Group by (provider_id, node_id, severity) so we can compute
    # success_pct per (provider, node). json_extract is supported by
    # the SQLAlchemy SQLite dialect.
    node_expr = func.json_extract(ActivityLog.event_meta, "$.node_id")
    rows = (await db.execute(
        select(
            ActivityLog.provider_id,
            node_expr.label("node_id"),
            ActivityLog.severity,
            func.count(),
        )
        .where(ActivityLog.created_at >= cutoff)
        .where(ActivityLog.provider_id.is_not(None))
        .group_by(ActivityLog.provider_id, "node_id", ActivityLog.severity)
    )).all()

    # Aggregate: { (provider_id, node_id) -> {info, warning, error, ...} }
    by_pn: dict[tuple, dict] = {}
    for prov_id, node_id, severity, count in rows:
        key = (prov_id, node_id or "unknown")
        bucket = by_pn.setdefault(key, {"info": 0, "warning": 0, "error": 0, "critical": 0, "total": 0})
        bucket[severity] = bucket.get(severity, 0) + count
        bucket["total"] += count

    # Pull provider name lookup
    provs = (await db.execute(
        select(Provider.id, Provider.name).where(Provider.deleted_at.is_(None))
    )).all()
    name_by_id = {pid: name for pid, name in provs}

    # Reshape into the response
    by_provider: dict[str, dict] = {}
    for (prov_id, node_id), bucket in by_pn.items():
        if not prov_id:
            continue
        entry = by_provider.setdefault(prov_id, {
            "provider_id": prov_id,
            "provider_name": name_by_id.get(prov_id, "(deleted)"),
            "by_node": {},
        })
        total = bucket["total"]
        succ = bucket.get("info", 0)
        entry["by_node"][node_id] = {
            "requests": total,
            "successes": succ,
            "success_pct": (succ / total * 100) if total else 0.0,
        }
    return list(by_provider.values())


@router.get("/{provider_id}/usage")
async def provider_usage(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.0.62: per-provider rolling usage windows (Phase 1 of usage-based
    rotation). Returns cached values from ``provider_usage_windows`` —
    populated by the ``usage_tracker`` background task every 60s for
    providers with ``usage_tracking_enabled=True``.

    Response shape:
      {
        provider_id, provider_name, tracking_enabled,
        session: {tokens, window_start, window_sec, limit_tokens, pct},
        weekly:  {tokens, reset_at, reset_dow, reset_hour, limit_tokens, pct},
        rotation_threshold_pct,
        updated_at,
      }

    If tracking is disabled, returns the config + null totals so the UI
    can show the "enable tracking" affordance.
    """
    res = await db.execute(select(Provider).where(
        Provider.id == provider_id, Provider.deleted_at.is_(None),
    ))
    p = res.scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "provider not found")

    # v3.9.8 — prefer ExternalUsageSnapshot (authoritative) over
    # ProviderUsageWindow (proxy-slice only). See providers.list_providers
    # docstring for the full rationale.
    snap_res = await db.execute(
        select(ExternalUsageSnapshot)
        .where(ExternalUsageSnapshot.provider_id == provider_id)
        .order_by(desc(ExternalUsageSnapshot.captured_at))
        .limit(1)
    )
    snap = snap_res.scalar_one_or_none()

    res2 = await db.execute(select(ProviderUsageWindow).where(
        ProviderUsageWindow.provider_id == provider_id,
    ))
    w = res2.scalar_one_or_none()

    if snap is not None and snap.seven_day_utilization is not None:
        # Authoritative path — scrape data.
        return {
            "provider_id": p.id,
            "provider_name": p.name,
            "tracking_enabled": bool(p.usage_tracking_enabled),
            "data_source": "external_scrape",
            "captured_at": utc_iso(snap.captured_at) if snap.captured_at else None,
            "auth_state": snap.auth_state,
            "session": {
                "tokens": None,  # not exposed by upstream scrape
                "window_start": None,
                "window_sec": p.usage_session_window_sec,
                "limit_tokens": p.usage_session_limit_tokens,
                "pct": snap.five_hour_utilization,
                "resets_at": utc_iso(snap.five_hour_resets_at) if snap.five_hour_resets_at else None,
            },
            "weekly": {
                "tokens": None,
                "reset_at": utc_iso(snap.seven_day_resets_at) if snap.seven_day_resets_at else None,
                "reset_dow": p.usage_weekly_reset_dow,
                "reset_hour": p.usage_weekly_reset_hour,
                "limit_tokens": p.usage_weekly_limit_tokens,
                "pct": snap.seven_day_utilization,
            },
            "rotation_threshold_pct": p.usage_rotation_threshold_pct,
            "updated_at": utc_iso(snap.captured_at) if snap.captured_at else None,
        }

    return {
        "provider_id": p.id,
        "provider_name": p.name,
        "tracking_enabled": bool(p.usage_tracking_enabled),
        "data_source": "internal_window" if w else None,
        "session": {
            "tokens": (w.session_tokens if w else 0),
            # v3.0.82: utc_iso() instead of naive ``isoformat() + "Z"``.
            # session_window_start is written by usage_tracker via
            # datetime.now(timezone.utc) so it's tz-aware; the bare
            # concat produced ``"2026-05-06T05:00:00+00:00Z"`` which JS
            # Date won't parse cleanly. Same fix as v3.0.73 utc_iso bug.
            "window_start": utc_iso(w.session_window_start) if w else None,
            "window_sec": p.usage_session_window_sec,
            "limit_tokens": p.usage_session_limit_tokens,
            "pct": (w.session_pct if w else None),
        },
        "weekly": {
            "tokens": (w.weekly_tokens if w else 0),
            "reset_at": utc_iso(w.weekly_reset_at) if w else None,
            "reset_dow": p.usage_weekly_reset_dow,
            "reset_hour": p.usage_weekly_reset_hour,
            "limit_tokens": p.usage_weekly_limit_tokens,
            "pct": (w.weekly_pct if w else None),
        },
        "rotation_threshold_pct": p.usage_rotation_threshold_pct,
        "updated_at": utc_iso(w.updated_at) if w else None,
    }
