"""v4.4 M-2 — read/write helpers for the ``provider_node_auth_state``
table.

Each proxy node OWNS the rows where ``node_id == settings.cluster_node_id``;
peer rows are read-only on this node (don't write to a peer's row from
here). The cluster sync layer propagates rows across the fleet.

The bridge calls ``write_local_state(provider_id, ...)`` from its own
status update path. The routing layer calls
``read_state(provider_id, node_id)`` to gate per-node grok-web
dispatch. The admin UI consumes ``read_all_states(provider_id)`` for
the per-node display.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db import ProviderNodeAuthState


AuthState = Literal["ok", "expired", "needs_reauth", "never_authed", "bridge_down"]


_VALID_AUTH_STATES = {"ok", "expired", "needs_reauth", "never_authed", "bridge_down"}


# Defensive cap for last_error so a flap doesn't bloat the cluster
# sync payload. Pre-fix BUG-091 (2026-05-06) had a similar issue with
# the activity_log blowing to 1 GB; ~400 chars matches that lesson.
_MAX_LAST_ERROR_CHARS = 400


def _own_node_id() -> str:
    return getattr(settings, "cluster_node_id", None) or "unknown-node"


async def write_local_state(
    db: AsyncSession,
    provider_id: str,
    auth_state: AuthState,
    last_ok_at: Optional[datetime] = None,
    reauth_url: Optional[str] = None,
    last_error: Optional[str] = None,
) -> None:
    """Upsert the row for (provider_id, this_node_id). Always sets
    ``last_check_at = now``. If ``auth_state == "ok"``, also bumps
    ``last_ok_at`` to now unless an explicit value is provided."""
    if auth_state not in _VALID_AUTH_STATES:
        raise ValueError(
            f"invalid auth_state {auth_state!r}; "
            f"expected one of {sorted(_VALID_AUTH_STATES)}"
        )
    now = datetime.utcnow()
    node_id = _own_node_id()
    err = (last_error or None)
    if err and len(err) > _MAX_LAST_ERROR_CHARS:
        err = err[:_MAX_LAST_ERROR_CHARS] + "…[truncated]"

    row = (await db.execute(
        select(ProviderNodeAuthState)
        .where(ProviderNodeAuthState.provider_id == provider_id)
        .where(ProviderNodeAuthState.node_id == node_id)
        .limit(1)
    )).scalar_one_or_none()

    if row is None:
        db.add(ProviderNodeAuthState(
            provider_id=provider_id,
            node_id=node_id,
            auth_state=auth_state,
            last_ok_at=(last_ok_at or (now if auth_state == "ok" else None)),
            last_check_at=now,
            reauth_url=reauth_url,
            last_error=err,
        ))
        return

    row.auth_state = auth_state
    row.last_check_at = now
    if auth_state == "ok":
        row.last_ok_at = last_ok_at or now
    elif last_ok_at is not None:
        row.last_ok_at = last_ok_at
    row.reauth_url = reauth_url
    row.last_error = err


async def read_state(
    db: AsyncSession, provider_id: str, node_id: Optional[str] = None
) -> Optional[ProviderNodeAuthState]:
    """Return the row for ``(provider_id, node_id)``. ``node_id``
    defaults to this proxy's own node — the routing path's typical
    consumer. ``None`` if no row has been written yet."""
    nid = node_id or _own_node_id()
    return (await db.execute(
        select(ProviderNodeAuthState)
        .where(ProviderNodeAuthState.provider_id == provider_id)
        .where(ProviderNodeAuthState.node_id == nid)
        .limit(1)
    )).scalar_one_or_none()


async def read_all_states(
    db: AsyncSession, provider_id: str
) -> list[ProviderNodeAuthState]:
    """Return every node's row for this provider. Used by the admin
    UI's per-node status display + by the per-node routing fallback
    ("none of the nodes' bridges are OK")."""
    rs = await db.execute(
        select(ProviderNodeAuthState)
        .where(ProviderNodeAuthState.provider_id == provider_id)
    )
    return list(rs.scalars().all())


def is_local_node_routable(state: Optional[ProviderNodeAuthState]) -> bool:
    """The routing-side filter: this node can serve the provider iff
    its local row says ``auth_state == "ok"``. Absence of a row
    (``state is None``) is treated as "not routable" — the bridge
    hasn't written a status yet, so we don't know.

    Path A §4.3 routing change uses this gate. For provider_types
    other than grok-web (no node-local-session semantics), the
    routing layer skips this filter entirely.
    """
    return state is not None and state.auth_state == "ok"
