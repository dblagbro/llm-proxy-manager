"""v3.7.27 (#245) — admin endpoints for the ChatGPT Plus / Codex Cloud
usage scraper. Mirrors ``app/api/anthropic_billing.py`` so the admin
UI can reuse rendering patterns.

Endpoints:

- ``POST /api/providers/{id}/codex-billing-credentials`` — paste a
  freshly-captured chatgpt.com cookie blob + the analytics endpoint
  URL captured from DevTools. Stores them on the Provider row.

- ``POST /api/providers/{id}/codex-billing-refresh`` — fire one scrape
  immediately (don't wait for the next 4h cycle). Useful for smoke-
  testing a freshly-pasted credential set.

External-usage retrieval is shared with the Anthropic path via
``GET /api/providers/{id}/external-usage`` (already defined in
``app/api/anthropic_billing.py``). Both Codex and Anthropic snapshots
land in ``external_usage_snapshot``, distinguished by ``source``.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.models.db import Provider

router = APIRouter(prefix="/api/providers", tags=["codex-billing"])


class CodexCredentialsBody(BaseModel):
    endpoint_url: str = Field(
        min_length=1,
        description=(
            "Analytics endpoint URL captured from chatgpt.com DevTools "
            "Network tab — typically an XHR call that returns the "
            "usage JSON when viewing /codex/cloud/settings/analytics"
        ),
    )
    cookies: str = Field(
        min_length=1,
        description=(
            "Cookie blob from chatgpt.com — JSON dict or "
            "'name=val; name=val' header style. Must include the "
            "NextAuth session token cookie."
        ),
    )


@router.post("/{provider_id}/codex-billing-credentials")
async def store_codex_credentials(
    provider_id: str,
    body: CodexCredentialsBody,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Store cookies + analytics endpoint URL for the periodic Codex
    billing scraper.

    Does NOT verify cookies actually work (that would couple this
    endpoint to a network call); operator uses the ``-refresh``
    endpoint to smoke-test.
    """
    from app.providers.codex_billing import parse_cookie_jar, validate_endpoint_url

    rs = await db.execute(select(Provider).where(Provider.id == provider_id))
    provider = rs.scalar_one_or_none()
    if not provider or provider.deleted_at is not None:
        raise HTTPException(404, "Provider not found")
    if provider.provider_type != "codex-oauth":
        raise HTTPException(
            400,
            f"codex billing scraper only applies to codex-oauth providers (got {provider.provider_type!r})",
        )
    url_err = validate_endpoint_url(body.endpoint_url.strip())
    if url_err:
        raise HTTPException(400, f"endpoint URL invalid: {url_err}")
    try:
        cookies = parse_cookie_jar(body.cookies)
    except ValueError as e:
        raise HTTPException(400, f"cookie blob invalid: {e}") from e
    provider.codex_usage_endpoint_url = body.endpoint_url.strip()
    provider.codex_session_cookies = json.dumps(cookies)
    provider.codex_session_captured_at = time.time()
    if hasattr(provider, "last_user_edit_at"):
        provider.last_user_edit_at = time.time()
    await db.commit()
    return {
        "ok": True,
        "provider_id": provider.id,
        "endpoint_url": provider.codex_usage_endpoint_url,
        "cookie_count": len(cookies),
        "captured_at": provider.codex_session_captured_at,
    }


@router.post("/{provider_id}/codex-billing-refresh")
async def codex_refresh_now(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    """Fire one Codex scrape immediately. Returns the result so the
    operator can verify a freshly-pasted credential set + endpoint URL
    work. Intentionally bypasses the worker's freshness guard — this
    endpoint is operator-triggered validation, not periodic scraping."""
    from app.providers.codex_billing import scrape_provider_into_snapshot

    rs = await db.execute(select(Provider).where(Provider.id == provider_id))
    provider = rs.scalar_one_or_none()
    if not provider or provider.deleted_at is not None:
        raise HTTPException(404, "Provider not found")
    if not provider.codex_usage_endpoint_url or not provider.codex_session_cookies:
        raise HTTPException(400, "no codex billing credentials configured for this provider")
    return await scrape_provider_into_snapshot(db, provider)
