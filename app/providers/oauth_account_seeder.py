"""v5.15.0 (#508 Phase 1) — Seed provider_oauth_accounts from legacy
Provider rows.

Idempotent one-shot at boot. For every OAuth-flavored Provider row that
holds a live access_token (``api_key``) AND has zero rows in the
child ``provider_oauth_accounts`` table, insert a single row copying
the current token pair. ``captured_via='migration_from_provider'`` so
the audit trail can identify seeded-vs-operator-added rows later.

Scope: v5.15.0 does NOT change dispatch — the seeded row exists so that
when Phase 2 (v5.15.1) flips dispatch to read the accounts table,
existing providers keep working with no operator action needed. This
seeder is the pre-cutover data-parity guarantee.

Rules:
- Only touches provider_types in ``_OAUTH_PROVIDER_TYPES`` (cursor-oauth,
  codex-oauth, claude-oauth). Non-OAuth providers stay untouched.
- Skips a provider if it already has ANY row in
  ``provider_oauth_accounts`` (regardless of enabled/deleted state). This
  is what makes the seeder safe to re-run on every boot.
- Skips a provider with a null/empty ``api_key`` (nothing to seed).
- Label defaults to the Provider's ``name`` — the operator can rename
  later via PATCH.
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Provider, ProviderOAuthAccount

logger = logging.getLogger(__name__)

_OAUTH_PROVIDER_TYPES = frozenset({
    "cursor-oauth",
    "codex-oauth",
    "claude-oauth",
})


async def seed_missing_accounts(session: AsyncSession) -> dict:
    """Idempotent seed. Returns count dict for boot logging + tests:

    ``{"scanned": int, "seeded": int, "skipped_no_token": int,
       "skipped_already_seeded": int}``
    """
    counts = {
        "scanned": 0,
        "seeded": 0,
        "skipped_no_token": 0,
        "skipped_already_seeded": 0,
    }

    result = await session.execute(
        select(Provider).where(Provider.provider_type.in_(_OAUTH_PROVIDER_TYPES))
    )
    providers = result.scalars().all()

    for prov in providers:
        counts["scanned"] += 1

        if not prov.api_key:
            counts["skipped_no_token"] += 1
            continue

        existing = await session.execute(
            select(ProviderOAuthAccount.id)
            .where(ProviderOAuthAccount.provider_id == prov.id)
            .limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            counts["skipped_already_seeded"] += 1
            continue

        acc = ProviderOAuthAccount(
            provider_id=prov.id,
            label=prov.name,
            access_token=prov.api_key,
            refresh_token=prov.oauth_refresh_token,
            oauth_expires_at=prov.oauth_expires_at,
            enabled=True,
            captured_via="migration_from_provider",
        )
        session.add(acc)
        counts["seeded"] += 1

    if counts["seeded"] > 0:
        await session.commit()
        logger.info(
            "oauth_account_seeder: seeded=%d scanned=%d skipped_no_token=%d skipped_already_seeded=%d",
            counts["seeded"], counts["scanned"],
            counts["skipped_no_token"], counts["skipped_already_seeded"],
        )
    return counts
