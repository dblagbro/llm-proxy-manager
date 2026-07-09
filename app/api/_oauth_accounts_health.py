"""v5.15.3 (#508 P1-5) — /health.oauthAccounts observability block.

Surfaces per-cluster snapshot of the OAuth fan-out state so operators
can answer: "is my picker actually rotating across accounts?" without
touching the DB.

Snapshot shape returned by ``snapshot_oauth_accounts()``::

    {
      "totalProviders": 3,        # OAuth-flavored + enabled
      "totalAccounts": 4,         # enabled child rows, non-deleted
      "providersWithMultiple": 1, # count where enabled_accounts >= 2
      "providersWithSingle": 2,   # count where enabled_accounts == 1
      "providersWithZero": 0,     # count where enabled_accounts == 0
      "rotationsLastHour": 5,     # distinct last_used_at bumps in 1h
      "byProviderType": {
        "cursor-oauth":     {"providers":2, "accounts":3, "rotations_1h":4},
        "codex-oauth":      {"providers":0, "accounts":0, "rotations_1h":0},
        "claude-oauth":     {"providers":1, "accounts":1, "rotations_1h":1},
      }
    }

Cached for 15s to avoid hitting the DB on every /health call — the
existing 3s health cache already excludes clusterSync + workers + this
block from its body; the 15s here is the second-order cheapness for
tight-loop probers.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Provider, ProviderOAuthAccount

_OAUTH_TYPES = ("cursor-oauth", "codex-oauth", "claude-oauth", "ChatGPT-oauth-plan")
_CACHE_TTL_SEC = 15.0

_cache: dict[str, Any] = {"ts": 0.0, "body": None}


async def snapshot_oauth_accounts(db: AsyncSession) -> dict:
    """Read + summarize. Best-effort; returns ``{"error": ...}`` on failure."""
    now = time.time()
    if _cache["body"] is not None and now - _cache["ts"] < _CACHE_TTL_SEC:
        return _cache["body"]

    try:
        one_hour_ago = now - 3600.0

        # Provider counts by type
        prov_q = (
            select(Provider.provider_type, func.count(Provider.id))
            .where(
                Provider.provider_type.in_(_OAUTH_TYPES),
                Provider.enabled == True,  # noqa: E712
            )
            .group_by(Provider.provider_type)
        )
        prov_rows = (await db.execute(prov_q)).all()
        provider_counts = {row[0]: int(row[1]) for row in prov_rows}

        # Account counts by provider type — join providers to accounts
        acc_q = (
            select(
                Provider.provider_type,
                func.count(ProviderOAuthAccount.id),
            )
            .join(
                ProviderOAuthAccount,
                ProviderOAuthAccount.provider_id == Provider.id,
            )
            .where(
                Provider.provider_type.in_(_OAUTH_TYPES),
                Provider.enabled == True,  # noqa: E712
                ProviderOAuthAccount.enabled == True,  # noqa: E712
                ProviderOAuthAccount.deleted_at.is_(None),
            )
            .group_by(Provider.provider_type)
        )
        acc_rows = (await db.execute(acc_q)).all()
        account_counts = {row[0]: int(row[1]) for row in acc_rows}

        # Rotations = accounts with last_used_at within past hour
        rot_q = (
            select(
                Provider.provider_type,
                func.count(ProviderOAuthAccount.id),
            )
            .join(
                ProviderOAuthAccount,
                ProviderOAuthAccount.provider_id == Provider.id,
            )
            .where(
                Provider.provider_type.in_(_OAUTH_TYPES),
                ProviderOAuthAccount.last_used_at.isnot(None),
                ProviderOAuthAccount.last_used_at >= one_hour_ago,
                ProviderOAuthAccount.deleted_at.is_(None),
            )
            .group_by(Provider.provider_type)
        )
        rot_rows = (await db.execute(rot_q)).all()
        rotation_counts = {row[0]: int(row[1]) for row in rot_rows}

        # Per-provider distribution (single vs multiple vs zero)
        dist_q = (
            select(Provider.id).where(
                Provider.provider_type.in_(_OAUTH_TYPES),
                Provider.enabled == True,  # noqa: E712
            )
        )
        provider_ids = [row[0] for row in (await db.execute(dist_q)).all()]

        with_multiple = 0
        with_single = 0
        with_zero = 0
        for pid in provider_ids:
            count_q = (
                select(func.count(ProviderOAuthAccount.id)).where(
                    ProviderOAuthAccount.provider_id == pid,
                    ProviderOAuthAccount.enabled == True,  # noqa: E712
                    ProviderOAuthAccount.deleted_at.is_(None),
                )
            )
            n = int((await db.execute(count_q)).scalar() or 0)
            if n >= 2:
                with_multiple += 1
            elif n == 1:
                with_single += 1
            else:
                with_zero += 1

        # Aggregate the by-type map with defaults for missing types.
        by_type = {}
        # Normalize ChatGPT-oauth-plan → codex-oauth in the surface so operators
        # don't have to know the v3.8.0 internal rename.
        def _bucket(t: str) -> str:
            return "codex-oauth" if t == "ChatGPT-oauth-plan" else t

        for t in ("cursor-oauth", "codex-oauth", "claude-oauth"):
            by_type[t] = {"providers": 0, "accounts": 0, "rotations_1h": 0}
        for t, n in provider_counts.items():
            by_type[_bucket(t)]["providers"] += n
        for t, n in account_counts.items():
            by_type[_bucket(t)]["accounts"] += n
        for t, n in rotation_counts.items():
            by_type[_bucket(t)]["rotations_1h"] += n

        body = {
            "totalProviders": len(provider_ids),
            "totalAccounts": sum(account_counts.values()),
            "providersWithMultiple": with_multiple,
            "providersWithSingle": with_single,
            "providersWithZero": with_zero,
            "rotationsLastHour": sum(rotation_counts.values()),
            "byProviderType": by_type,
        }
        _cache["ts"] = now
        _cache["body"] = body
        return body
    except Exception as e:
        return {"error": str(e)[:200]}


def reset_cache_for_tests() -> None:
    _cache["ts"] = 0.0
    _cache["body"] = None
