"""v3.7.0 — admin endpoints for the Anthropic Console billing scraper.

Three endpoints, all admin-gated:

- ``POST /api/providers/{id}/anthropic-billing-credentials`` — paste a
  freshly-captured browser cookie blob + org UUID. Validates and
  stores them on the Provider row. Cookies expire ~30 days; this is
  the operator action when the worker emits an
  ``auth_state=session_expired`` event.

- ``POST /api/providers/{id}/anthropic-billing-refresh`` — fire one
  scrape immediately (don't wait for the next 4h cycle). Useful for
  smoke-testing a freshly-pasted credential set.

- ``GET /api/providers/{id}/external-usage`` — return the most recent
  N snapshots for a provider so the dashboard / operator can see
  what's been collected.
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import ExternalUsageSnapshot, Provider

router = APIRouter(prefix="/api/providers", tags=["anthropic-billing"])


class CredentialsBody(BaseModel):
    org_uuid: str = Field(min_length=1, description="Anthropic organization UUID — visible in the captured /usage URL or in the lastActiveOrg cookie value")
    cookies: str = Field(min_length=1, description="Cookie blob — JSON dict, or 'name=val; name=val' header style")


@router.post("/{provider_id}/anthropic-billing-credentials")
async def store_credentials(
    provider_id: str,
    body: CredentialsBody,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Store cookies + org UUID for the periodic billing scraper.

    Pre-validates the cookie blob — rejects with 400 if required
    cookies (``sessionKey``) are missing. Does NOT verify the cookies
    actually work (that would couple this endpoint to a network call);
    the operator uses the ``-refresh`` endpoint to smoke-test.
    """
    from app.providers.anthropic_billing import parse_cookie_jar, validate_cookies

    rs = await db.execute(select(Provider).where(Provider.id == provider_id))
    provider = rs.scalar_one_or_none()
    if not provider or provider.deleted_at is not None:
        raise HTTPException(404, "Provider not found")
    if provider.provider_type != "claude-oauth":
        raise HTTPException(
            400, f"billing scraper only applies to claude-oauth providers (got {provider.provider_type!r})",
        )
    try:
        cookies = parse_cookie_jar(body.cookies)
    except ValueError as e:
        raise HTTPException(400, f"cookie blob invalid: {e}") from e
    cookie_err = validate_cookies(cookies)
    if cookie_err:
        raise HTTPException(400, f"cookies insufficient: {cookie_err}")
    # Store the normalized JSON form so the worker doesn't have to
    # re-parse a different shape on every call.
    import json
    provider.anthropic_org_uuid = body.org_uuid.strip()
    provider.anthropic_session_cookies = json.dumps(cookies)
    provider.anthropic_session_captured_at = time.time()
    provider.last_user_edit_at = time.time()
    await db.commit()
    return {
        "ok": True,
        "provider_id": provider.id,
        "org_uuid": provider.anthropic_org_uuid,
        "cookie_count": len(cookies),
        "captured_at": provider.anthropic_session_captured_at,
    }


@router.post("/{provider_id}/anthropic-billing-refresh")
async def refresh_now(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Fire one scrape immediately. Returns the result so operator
    can verify a freshly-pasted credential set works."""
    from app.providers.anthropic_billing import scrape_provider_into_snapshot

    rs = await db.execute(select(Provider).where(Provider.id == provider_id))
    provider = rs.scalar_one_or_none()
    if not provider or provider.deleted_at is not None:
        raise HTTPException(404, "Provider not found")
    if not provider.anthropic_org_uuid or not provider.anthropic_session_cookies:
        raise HTTPException(400, "no anthropic billing credentials configured for this provider")
    return await scrape_provider_into_snapshot(db, provider)


@router.post("/_evaluate-rotation-rules")
async def evaluate_rotation_now(
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.7.1 — fire the auto-rotation rule evaluator across every
    claude-oauth provider using their latest snapshot. Used to apply
    rules immediately after a credential paste, or any time the
    operator wants to force a re-evaluation without waiting for the
    next 4-hour scrape cycle.

    Returns the decision dict for each provider so the operator can
    see exactly what changed (or didn't).
    """
    from app.routing.external_rotation import evaluate_rules_for_all_providers
    decisions = await evaluate_rules_for_all_providers(db)
    return {
        "evaluated": len(decisions),
        "decisions": decisions,
    }


@router.get("/{provider_id}/external-usage")
async def list_snapshots(
    provider_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Return the most-recent N snapshots for a provider, newest first.

    Includes failure rows (auth_state != 'ok') so the operator can see
    when cookies expired without scrolling through the activity log.
    """
    rs = await db.execute(
        select(ExternalUsageSnapshot)
        .where(ExternalUsageSnapshot.provider_id == provider_id)
        .order_by(desc(ExternalUsageSnapshot.captured_at))
        .limit(limit)
    )
    rows = rs.scalars().all()
    return [
        {
            "id": r.id,
            "captured_at": r.captured_at.isoformat() if r.captured_at else None,
            "source": r.source,
            "http_status": r.http_status,
            "auth_state": r.auth_state,
            "error": r.error,
            "five_hour_utilization": r.five_hour_utilization,
            "five_hour_resets_at": r.five_hour_resets_at.isoformat() if r.five_hour_resets_at else None,
            "seven_day_utilization": r.seven_day_utilization,
            "seven_day_resets_at": r.seven_day_resets_at.isoformat() if r.seven_day_resets_at else None,
            "seven_day_sonnet_utilization": r.seven_day_sonnet_utilization,
            "seven_day_opus_utilization": r.seven_day_opus_utilization,
            "extra_usage_is_enabled": r.extra_usage_is_enabled,
            "extra_usage_monthly_limit": r.extra_usage_monthly_limit,
            "extra_usage_used_credits": r.extra_usage_used_credits,
            "extra_usage_utilization": r.extra_usage_utilization,
            "extra_usage_currency": r.extra_usage_currency,
        }
        for r in rows
    ]
