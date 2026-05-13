"""v3.8.1 (#245 Phase 2) — admin endpoints for ChatGPT/Codex usage scrape.

Phase 2 of #245 drops the operator-cookie-paste flow that Phase 1
shipped: the scraper now uses the existing OAuth access_token
(Provider.api_key) that the codex-oauth refresh flow already maintains.
No operator action needed beyond standard ChatGPT-oauth-plan login.

Endpoints:
- POST /api/providers/{id}/codex-billing-refresh — manual smoke-test
  trigger; fires one scrape immediately.
- POST /api/providers/{id}/codex-billing-credentials — kept as a
  no-op compatibility endpoint so prior callers don't 404. Logs a
  deprecation warning. Will be removed in a future major version.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import Provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/providers", tags=["codex-billing"])


class _LegacyCredentialsBody(BaseModel):
    endpoint_url: str = Field(default="", description="(deprecated) ignored — endpoint hardcoded since v3.8.1")
    cookies: str = Field(default="", description="(deprecated) ignored — uses OAuth access_token since v3.8.1")


@router.post("/{provider_id}/codex-billing-credentials")
async def store_codex_credentials_legacy(
    provider_id: str,
    body: _LegacyCredentialsBody,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """v3.8.1 (#245 Phase 2): no longer requires cookies/endpoint —
    the scrape uses the existing OAuth access_token. This endpoint
    remains as a compatibility shim returning a clear deprecation
    message so prior callers don't 404 hard."""
    rs = await db.execute(select(Provider).where(Provider.id == provider_id))
    provider = rs.scalar_one_or_none()
    if not provider or provider.deleted_at is not None:
        raise HTTPException(404, "Provider not found")
    logger.info(
        "codex_billing.credentials_endpoint_called_deprecated provider_id=%s",
        provider_id,
    )
    return {
        "ok": True,
        "deprecated": True,
        "message": (
            "Codex billing scrape no longer requires pasted cookies/endpoint "
            "since v3.8.1. It uses the provider's OAuth access_token "
            "(maintained by the existing codex-oauth refresh flow). "
            "Use POST /codex-billing-refresh to fire a manual scrape."
        ),
        "has_oauth_token": bool(provider.api_key),
    }


@router.post("/{provider_id}/codex-billing-refresh")
async def codex_refresh_now(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Fire one Codex scrape immediately. Operator-initiated, bypasses
    the worker's freshness guard. Useful for smoke-testing after
    enabling a new ChatGPT-oauth-plan provider or after re-auth."""
    from app.providers.codex_billing import scrape_provider_into_snapshot

    rs = await db.execute(select(Provider).where(Provider.id == provider_id))
    provider = rs.scalar_one_or_none()
    if not provider or provider.deleted_at is not None:
        raise HTTPException(404, "Provider not found")
    if provider.provider_type != "ChatGPT-oauth-plan":
        raise HTTPException(
            400,
            f"codex billing scraper only applies to ChatGPT-oauth-plan providers (got {provider.provider_type!r})",
        )
    if not provider.api_key:
        raise HTTPException(
            400,
            "provider has no OAuth access_token — complete the ChatGPT-oauth-plan login flow first",
        )
    return await scrape_provider_into_snapshot(db, provider)
