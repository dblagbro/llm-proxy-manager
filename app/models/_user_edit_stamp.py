"""
SQLAlchemy event listener that auto-bumps ``Provider.last_user_edit_at``
when admin-meaningful columns change.

Background
----------

v3.0.11 introduced ``last_user_edit_at`` as a "real admin edit"
timestamp distinct from ``updated_at``. The intent: cluster sync's
LWW prefers ``last_user_edit_at`` so background mutations on a peer
(OAuth auto-refresh, deprecation auto-bump, priority tie-break)
can't revert a real admin edit on this node.

The discipline was: admin-facing API handlers in ``app/api/providers.py``
explicitly call ``_stamp_user_edit(p)`` before commit. That works as
long as every code path goes through those handlers — but doesn't
help when an admin script touches the DB directly, or when a future
handler is added without remembering to stamp.

v3.2.7 surfaced the gap: an ``extra_config.bridge_url`` change made
via ``sudo docker exec ... python3`` bumped ``updated_at`` (via the
SQLAlchemy ``onupdate`` hook on the column) but NOT
``last_user_edit_at`` — so peer LWW comparisons went into a tied
state and rejected the change. v3.2.7 fixed the comparison logic;
this module fixes the SOURCE so the stamp is always honored.

What this listener does
-----------------------

Hooks into the ORM ``before_update`` event for ``Provider``. If any
of the user-meaningful columns has been touched by the current
transaction, set ``last_user_edit_at = time.time()``.

What it does NOT do
-------------------

Doesn't bump on changes to columns that are explicitly background
machinery:
  - ``updated_at`` — the trigger we're trying to distinguish from
  - ``api_key`` / ``oauth_refresh_token`` / ``oauth_expires_at`` —
    these get rotated by OAuth refresh flows and shouldn't count as
    "admin edits"
  - ``deleted_at`` — handled separately (tombstone replication is
    its own LWW path)
  - ``last_user_edit_at`` — to avoid recursion if a caller already
    set it explicitly

If a real admin edit happens to ALSO touch one of those (e.g. admin
manually pastes a new api_key), the dedicated handler in
``app/api/providers.py`` calls ``_stamp_user_edit`` directly. So:
explicit stamp from handler beats this listener; this listener is
strictly the safety net.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import event, inspect
from sqlalchemy.orm.attributes import History
from sqlalchemy.orm.session import Session

from app.models.db import Provider

logger = logging.getLogger(__name__)

# Columns whose change should NOT trigger a user-edit stamp.
# Everything else on Provider is "user-meaningful" (rename, retype,
# api_base, default_model, priority, enabled, timeout, hold_down_sec,
# failure_threshold, daily_budget_usd, extra_config, owned_by_key_id,
# cost_class, usage_*).
_BACKGROUND_COLUMNS = frozenset({
    "updated_at",
    "created_at",
    "last_user_edit_at",
    "api_key",
    "oauth_refresh_token",
    "oauth_expires_at",
    "deleted_at",
})


def _has_user_edit(target: Provider) -> bool:
    """Inspect the SQLAlchemy attribute history; return True if any
    non-background column has a pending change."""
    insp = inspect(target)
    for attr in insp.attrs:
        if attr.key in _BACKGROUND_COLUMNS:
            continue
        hist: History = attr.history
        if hist.has_changes():
            return True
    return False


@event.listens_for(Provider, "before_update", propagate=True)
def _bump_user_edit_at_on_meaningful_change(mapper, connection, target):
    """Fired before each UPDATE; mutates target if the row has a
    user-meaningful change pending and the caller didn't already set
    ``last_user_edit_at`` themselves in this transaction."""
    insp = inspect(target)
    # If caller explicitly set last_user_edit_at in this transaction,
    # respect that — they may be importing data with a specific
    # historical timestamp and we don't want to clobber it.
    if insp.attrs.last_user_edit_at.history.has_changes():
        return
    if not _has_user_edit(target):
        return
    new_ts = time.time()
    target.last_user_edit_at = new_ts
    logger.debug(
        "auto-bumped Provider.last_user_edit_at id=%s ts=%s reason=column_change",
        target.id, new_ts,
    )
