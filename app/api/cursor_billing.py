"""v5.3.5 — admin endpoints for Cursor dashboard usage scrape.

Backend parity ship. v4.4.41 added ``cursor_billing.py`` scraper +
worker (4h cadence) but never wired the operator-facing manual-trigger
UI. This module brings Cursor to parity with Anthropic + Codex:

Endpoints:
- POST /api/providers/{id}/cursor-billing-refresh — single-provider
  manual scrape trigger. Fires one ``scrape_provider_into_snapshot``
  immediately, bypassing the worker's freshness floor. Useful right
  after re-auth or to confirm a Pro upgrade has propagated.
- POST /api/providers/_refresh-all-cursor-billing — bulk equivalent
  to the anthropic-billing flavour. Fans out across every
  cursor-oauth provider with a stored OAuth access_token.

Authentication: existing OAuth access_token in ``Provider.api_key``,
populated by the cursor-oauth login flow. No cookie paste required
(parallel to codex_billing v3.8.1 design).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import Provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/providers", tags=["cursor-billing"])


@router.post("/{provider_id}/cursor-billing-refresh")
async def cursor_refresh_now(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Fire one Cursor dashboard scrape immediately. Returns the result
    so operators can verify a freshly-pasted OAuth token works (or
    confirm a Pro upgrade on the account propagated to Cursor's API).
    """
    from app.providers.cursor_billing import scrape_provider_into_snapshot

    rs = await db.execute(select(Provider).where(Provider.id == provider_id))
    provider = rs.scalar_one_or_none()
    if not provider or provider.deleted_at is not None:
        raise HTTPException(404, "Provider not found")
    if provider.provider_type != "cursor-oauth":
        raise HTTPException(
            400,
            f"cursor billing scraper only applies to cursor-oauth providers "
            f"(got {provider.provider_type!r})",
        )
    if not provider.api_key:
        raise HTTPException(
            400,
            "provider has no OAuth access_token — complete the cursor-oauth "
            "login flow first",
        )
    return await scrape_provider_into_snapshot(db, provider)


@router.post("/_refresh-all-cursor-billing")
async def refresh_all_cursor_billing(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v5.3.5 — fire a cursor billing scrape for EVERY cursor-oauth
    provider that has an OAuth access_token, ignoring the worker's
    freshness floor.

    Mirrors ``_refresh-all-anthropic-billing``. Each scrape updates
    ``external_usage_snapshot`` and re-evaluates the rotation rules,
    so a provider whose utilization dropped below the at-capacity
    threshold has its ``auto_skip_until`` cleared and is returned to
    service immediately, without waiting for the next 4-hour cycle.

    Returns per-provider results so the operator can see which accounts
    were refreshed and which (if any) came back into service.
    """
    from app.providers.cursor_billing import scrape_provider_into_snapshot

    rs = await db.execute(
        select(Provider)
        .where(Provider.provider_type == "cursor-oauth")
        .where(Provider.deleted_at.is_(None))
        .where(Provider.api_key.is_not(None))
    )
    providers = rs.scalars().all()

    results: list[dict] = []
    scraped_ok = 0
    returned_to_service = 0
    for provider in providers:
        try:
            r = await scrape_provider_into_snapshot(db, provider)
        except Exception as exc:  # noqa: BLE001 — one bad provider must not abort the sweep
            results.append({
                "provider_id": provider.id,
                "provider_name": provider.name,
                "ok": False,
                "error": str(exc),
            })
            continue
        decision = (r.get("rotation_decision") or {}).get("decision")
        ok = bool(r.get("ok", False))
        if ok:
            scraped_ok += 1
        if decision == "returned_to_service":
            returned_to_service += 1
        results.append({
            "provider_id": provider.id,
            "provider_name": provider.name,
            "ok": ok,
            "auth_state": r.get("auth_state"),
            "seven_day_utilization": r.get("seven_day_utilization"),
            "rotation_decision": decision,
        })
    return {
        "providers": len(providers),
        "scraped_ok": scraped_ok,
        "returned_to_service": returned_to_service,
        "results": results,
    }
