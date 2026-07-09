"""v5.15.1 Phase 2 (#508) — per-account OAuth token selector.

Called at dispatch time to pick which account's access_token to use for
a request. Callers today:
- ``app/api/completions.py`` cursor-oauth path
- ``app/api/messages.py`` cursor-oauth path (via _messages_dispatch)

Selection strategy is per-provider via ``Provider.oauth_account_strategy``;
when NULL the app-wide default kicks in
(``settings.oauth_account_default_strategy``, default ``least_utilized``).

Strategies:

- ``least_utilized`` — pick the enabled, non-deleted account with the
  lowest ``utilization_pct``. Ties broken by oldest ``last_used_at``,
  then oldest ``created_at``. This is the default because Cursor's real
  bottleneck is per-account utilization; spreading requests to the
  quietest account maximizes the time between forced re-auths.

- ``round_robin`` / ``least_recently_used`` (aliases) — pick the enabled
  account with oldest ``last_used_at``. Simpler; doesn't depend on the
  billing worker having populated ``utilization_pct`` recently.

After picking, we do a fire-and-forget UPDATE of ``last_used_at`` so
subsequent calls advance the rotation. The update is not held across
the upstream call — no risk of extending the DB session's lifetime past
the dispatch handler.

Fallback: when the feature is disabled (``settings.oauth_account_fanout_enabled=False``)
or the provider has zero enabled accounts, the selector returns the
legacy ``Provider.api_key`` and provider is used as-is. This is the
"safe revert" path — Phase 1's seeder already ensured every OAuth
provider has at least one account row, but the legacy fallback is
kept as belt-and-braces so a stale-cache or migration-skipped provider
doesn't break dispatch.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db import Provider, ProviderOAuthAccount

logger = logging.getLogger(__name__)

VALID_STRATEGIES = frozenset({
    "least_utilized",
    "round_robin",
    "least_recently_used",
})


def resolve_strategy(provider: Provider) -> str:
    """Provider-set override wins; otherwise fall back to app-wide default.
    Unknown strategy strings degrade to ``least_utilized`` so a typo in
    the DB doesn't break dispatch."""
    s = getattr(provider, "oauth_account_strategy", None)
    if s and s in VALID_STRATEGIES:
        return s
    default = getattr(
        settings, "oauth_account_default_strategy", "least_utilized",
    )
    return default if default in VALID_STRATEGIES else "least_utilized"


async def _pick_by_strategy(
    db: AsyncSession, provider_id: str, strategy: str,
) -> Optional[ProviderOAuthAccount]:
    """Return the account to use, or None if no enabled accounts exist.
    NEVER returns a deleted account; NEVER returns a disabled account."""
    q = select(ProviderOAuthAccount).where(
        ProviderOAuthAccount.provider_id == provider_id,
        ProviderOAuthAccount.enabled == True,  # noqa: E712
        ProviderOAuthAccount.deleted_at.is_(None),
    )
    if strategy == "least_utilized":
        # NULL utilization_pct sorts as 0 in SQLite; a fresh account with
        # no billing data yet is preferred, which is correct — we want
        # to distribute load to the least-used slot.
        q = q.order_by(
            ProviderOAuthAccount.utilization_pct.asc(),
            ProviderOAuthAccount.last_used_at.asc().nulls_first(),
            ProviderOAuthAccount.created_at.asc(),
        )
    else:  # round_robin / least_recently_used
        q = q.order_by(
            ProviderOAuthAccount.last_used_at.asc().nulls_first(),
            ProviderOAuthAccount.created_at.asc(),
        )
    q = q.limit(1)
    result = await db.execute(q)
    return result.scalar_one_or_none()


async def _touch_last_used(
    db: AsyncSession, account_id: str,
) -> None:
    """Fire-and-forget UPDATE of last_used_at. Swallowed on failure
    (metric-shaped write, dispatch must not fail on a metric write)."""
    try:
        now_ts = time.time()
        await db.execute(
            update(ProviderOAuthAccount)
            .where(ProviderOAuthAccount.id == account_id)
            .values(last_used_at=now_ts)
        )
        await db.commit()
    except Exception as e:
        logger.debug(
            "oauth_account_selector.touch_last_used_failed account=%s err=%s",
            account_id, e,
        )


_OAUTH_PROVIDER_TYPES = frozenset({
    "cursor-oauth",
    "codex-oauth",
    "claude-oauth",
})


async def apply_fanout_to_kwargs(
    kwargs: dict, provider: Provider, db: AsyncSession,
) -> Optional[str]:
    """Mutate ``kwargs['api_key']`` in-place if the provider is an OAuth
    type AND has enabled accounts AND fan-out is enabled. Returns the
    ``account_id`` used (or ``None`` if legacy fallback was taken).

    Safe to call for any provider — non-OAuth providers early-return
    without touching kwargs or the DB. This is the callsite hook the
    ``messages.py`` / ``completions.py`` / ``_messages_dispatch`` dispatch
    sites use to flip on the v5.15.1 fan-out with a single line.
    """
    if provider.provider_type not in _OAUTH_PROVIDER_TYPES:
        return None
    if not getattr(settings, "oauth_account_fanout_enabled", True):
        return None
    strategy = resolve_strategy(provider)
    account = await _pick_by_strategy(db, provider.id, strategy)
    if account is None:
        return None
    kwargs["api_key"] = account.access_token
    await _touch_last_used(db, account.id)
    return account.id


async def resolve_access_token(
    provider: Provider, db: AsyncSession,
) -> Tuple[str, Optional[str]]:
    """Return ``(access_token, account_id)`` for a dispatch call.

    - When fan-out is disabled OR no enabled accounts exist → returns
      ``(provider.api_key, None)`` — legacy behavior.
    - When an account IS picked → returns
      ``(account.access_token, account.id)`` and touches ``last_used_at``.

    Callers use ``account_id`` in log lines and, later, Phase 3 per-account
    utilization tracking. Phase 2 callers can ignore it.
    """
    if not getattr(settings, "oauth_account_fanout_enabled", True):
        return (provider.api_key or "", None)

    strategy = resolve_strategy(provider)
    account = await _pick_by_strategy(db, provider.id, strategy)
    if account is None:
        return (provider.api_key or "", None)

    await _touch_last_used(db, account.id)
    return (account.access_token, account.id)
