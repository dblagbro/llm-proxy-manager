"""Read-only admin endpoints reachable via cluster HMAC auth (v4.3.5).

Built for the coordinator-hub team's 2026-05-20 request: surface
per-Anthropic-account weekly utilization on the hub UI without holding
an admin session cookie on the proxy side.

Currently one endpoint:

- ``GET /api/admin/external-usage-summary`` — latest
  ``external_usage_snapshot`` row per provider (whether the scrape
  succeeded or failed), keyed by provider, so the hub can surface
  weekly + 5-hour utilization plus auth_state per Anthropic account.

Auth: HMAC over ``(timestamp + path + body)`` per
``app.auth.cluster_hmac``. The hub already holds the shared secret as
``COORDINATOR_HMAC_KEY``; the proxy reads it from the same env var.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.cluster_hmac import require_cluster_hmac
from app.models.database import get_db
from app.models.db import ExternalUsageSnapshot, Provider
from app.utils.timefmt import utc_iso


router = APIRouter(prefix="/api/admin", tags=["admin-cluster-read"])


@router.get(
    "/external-usage-summary",
    summary="Latest external_usage_snapshot per provider (HMAC-auth)",
)
async def external_usage_summary(
    db: AsyncSession = Depends(get_db),
    _hmac: bool = Depends(require_cluster_hmac),
):
    """Return the latest ``external_usage_snapshot`` row per provider.

    Includes failure rows (``auth_state != 'ok'``) so the operator can
    see when cookies expired or the scraper hit Cloudflare without
    scrolling through the activity log.

    **Utilization scale (v4.3.5 contract clarification, 2026-05-20):**
    ``seven_day_utilization`` and ``five_hour_utilization`` are
    **percent values (0.0 – 100.0)**, NOT ratios. This matches the
    DB column convention (``Float, # percent 0-100`` in
    ``ExternalUsageSnapshot``) and the rest of the codebase
    (``external_rotation`` rules + the proxy admin UI all treat the
    value as percent). The original v4.3.5 hub-team memo described
    ratios — that was a documentation error in the memo; the
    endpoint never emitted ratios. Hub-side consumers should display
    the value as ``f"{value:.1f}%"`` (no ×100 step).
    """
    # latest snapshot id per provider via group-by-then-join
    latest_subq = (
        select(
            ExternalUsageSnapshot.provider_id.label("pid"),
            func.max(ExternalUsageSnapshot.captured_at).label("captured_at"),
        )
        .group_by(ExternalUsageSnapshot.provider_id)
        .subquery()
    )
    q = (
        select(ExternalUsageSnapshot, Provider)
        .join(
            latest_subq,
            (ExternalUsageSnapshot.provider_id == latest_subq.c.pid)
            & (ExternalUsageSnapshot.captured_at == latest_subq.c.captured_at),
        )
        .join(Provider, Provider.id == ExternalUsageSnapshot.provider_id)
        .where(Provider.deleted_at.is_(None))
    )
    result = await db.execute(q)

    accounts = []
    latest_overall = None
    for snap, prov in result.all():
        accounts.append(
            {
                "label": prov.name,
                "provider_id": prov.id,
                "seven_day_utilization": snap.seven_day_utilization,
                "five_hour_utilization": snap.five_hour_utilization,
                "auth_state": snap.auth_state,
                "last_scrape_at": utc_iso(snap.captured_at)
                if snap.captured_at
                else None,
            }
        )
        if snap.captured_at and (
            latest_overall is None or snap.captured_at > latest_overall
        ):
            latest_overall = snap.captured_at

    return {
        "snapshot_at": utc_iso(latest_overall) if latest_overall else None,
        "accounts": accounts,
    }
