"""Upstream model-deprecation registry + auto-migration.

Vendors retire model IDs on their own schedule (Google killed
``gemini-2.0-flash`` and ``gemini-1.5-*`` in 2026; Anthropic retires older
Sonnet/Opus snapshots periodically; OpenAI deprecates `gpt-4-turbo` etc).
When a provider's ``default_model`` references a retired ID, every
test/scan/real-traffic call surfaces the same not-found error and the
operator has to fix each provider one at a time.

This module fixes that at the root:

  1. Hard-coded ``MODEL_DEPRECATIONS`` registry — deprecated → replacement
  2. ``migrate_deprecated_default_models(db)`` runs once at boot and
     bumps every Provider row's ``default_model`` from a deprecated ID
     to its replacement. Idempotent.
  3. ``check_model_deprecation(model)`` — returns the replacement id
     (or None) for a single model name. Used by the scan-models +
     test-provider response builders to surface deprecation warnings
     in the UI BEFORE the operator hits a 404.

The registry is updated in-tree as vendors announce retirements. New
entries on top, with the announcement date for traceability. Operators
can also add custom mappings via the admin Settings page (TODO: future
runtime extension if we ever need it; hard-coded is the right default).
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Provider

logger = logging.getLogger(__name__)


# Deprecated model IDs → replacement IDs. Comment with the announcement
# date so we can prune entries 6+ months after Google/Anthropic actually
# return 404 (the "no longer available to new users" 404 is a hard signal,
# not an aspiration).
MODEL_DEPRECATIONS: dict[str, str] = {
    # ── Google Gemini retirements (2026 sweep) ──
    "gemini-2.0-flash":           "gemini-2.5-flash",
    "gemini-2.0-flash-exp":       "gemini-2.5-flash",
    "gemini-2.0-pro":             "gemini-2.5-pro",
    "gemini-2.0-pro-exp":         "gemini-2.5-pro",
    "gemini-1.5-flash":           "gemini-2.5-flash",
    "gemini-1.5-flash-8b":        "gemini-2.5-flash",
    "gemini-1.5-pro":             "gemini-2.5-pro",
    "models/gemini-2.0-flash":    "gemini-2.5-flash",  # `models/` prefix variant
    "models/gemini-1.5-flash":    "gemini-2.5-flash",
    "models/gemini-1.5-pro":      "gemini-2.5-pro",
    # gemini/-prefix variants (litellm style)
    "gemini/gemini-2.0-flash":    "gemini/gemini-2.5-flash",
    "gemini/gemini-1.5-flash":    "gemini/gemini-2.5-flash",
    "gemini/gemini-1.5-pro":      "gemini/gemini-2.5-pro",

    # ── Anthropic Claude retirements (older snapshots) ──
    # Anthropic publishes the active model list at api.anthropic.com/v1/models
    # so check there before adding entries here. These are well-known
    # retirements as of early 2026.
    "claude-3-5-sonnet-20241022": "claude-sonnet-4-6",
    "claude-3-5-sonnet-20240620": "claude-sonnet-4-6",
    "claude-3-opus-20240229":     "claude-opus-4-7",
    "claude-3-haiku-20240307":    "claude-haiku-4-5",
    # anthropic/-prefix variants (litellm style)
    "anthropic/claude-3-5-sonnet-20241022": "anthropic/claude-sonnet-4-6",
    "anthropic/claude-3-opus-20240229":     "anthropic/claude-opus-4-7",
    "anthropic/claude-3-haiku-20240307":    "anthropic/claude-haiku-4-5",

    # ── OpenAI retirements ──
    "gpt-4-turbo":             "gpt-4o",
    "gpt-4-turbo-preview":     "gpt-4o",
    "gpt-4-0125-preview":      "gpt-4o",
    "gpt-3.5-turbo-16k":       "gpt-4o-mini",
    "openai/gpt-4-turbo":      "openai/gpt-4o",
    "openai/gpt-3.5-turbo-16k": "openai/gpt-4o-mini",
}


def check_model_deprecation(model: Optional[str]) -> Optional[str]:
    """Return the replacement model id if ``model`` is deprecated, else None.

    Used by /api/providers/{id}/test and /api/providers/{id}/scan-models
    to surface a deprecation warning in the UI alongside a successful
    scan, so operators can update the default before the upstream 404s.
    """
    if not model:
        return None
    return MODEL_DEPRECATIONS.get(model)


async def migrate_deprecated_default_models(db: AsyncSession) -> dict[str, str]:
    """Boot-time migration: bump every provider's ``default_model`` from a
    deprecated id to its registered replacement. Returns a map of
    {provider_id: "old_model → new_model"} for whatever changed.

    Idempotent — running on every startup is safe; the second pass
    finds zero matches in MODEL_DEPRECATIONS for the already-migrated
    rows.
    """
    if not MODEL_DEPRECATIONS:
        return {}

    result = await db.execute(
        select(Provider).where(
            Provider.default_model.in_(list(MODEL_DEPRECATIONS.keys())),
            Provider.deleted_at.is_(None),
        )
    )
    changed: dict[str, str] = {}
    for prov in result.scalars().all():
        old = prov.default_model
        new = MODEL_DEPRECATIONS.get(old)
        if not new or new == old:
            continue
        prov.default_model = new
        changed[prov.id] = f"{old} → {new}"
        logger.info(
            "providers.deprecation_migration provider=%s name=%r old=%r new=%r",
            prov.id, prov.name, old, new,
        )
    if changed:
        await db.commit()
    return changed
