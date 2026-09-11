"""claude-oauth proactive token refresh (v5.22.18).

Why this exists
---------------
claude-oauth access tokens live ~8 hours. Until now the ONLY thing that
refreshed them was the lazy-on-401 path inside request dispatch
(``app/api/_messages_streaming_oauth.py``, two call sites, both gated on
``r.status_code == 401 and not refreshed``). Nothing refreshed a token
proactively.

That interacts fatally with the circuit breaker. ``record_auth_failure`` sets
``hold_down_until = now + 86400``, and ``is_available()`` returns False for
the whole hold-down — so an auth-failed provider is removed from routing for
24 hours. During those 24 hours no request is dispatched, so no 401 is seen,
so the refresh never runs. The token only gets staler. When the hold-down
lifts, the first probe 401s and, if that one inline shot does not succeed, the
breaker re-opens for another 24 hours.

    auth failure -> breaker open 24h -> no requests -> no 401 -> no refresh
        -> staler token -> hold-down lifts -> 401 -> breaker open 24h -> ...

A provider cannot escape that loop no matter how valid its refresh token is.
Observed 2026-09-10: ``Devin-Anthropic-Max-VG`` and
``Devin-Anthropic-Max-Gmail`` both sat in 24h hold-down with perfectly good
108-character refresh tokens in the DB. VG had not refreshed since 2026-09-08
18:05 — two full cycles — and the cluster showed 5/7 and 4/7 providers.

The same failure mode was already found and fixed for Cursor:
``cursor_oauth_expiry_monitor``'s own docstring says it "proactively refreshes
tokens within 24h of expiry (**was lazy-on-401**)". claude-oauth never got the
counterpart. This is that counterpart.

Two things make it break the deadlock rather than just paper over it:

1. It runs on a timer, wholly outside request dispatch, so an open breaker
   cannot stop it.
2. On a successful refresh it clears the auth-failure state and force-closes
   the breaker. A fresh token behind a 24-hour open breaker is still a dead
   provider, so refreshing without releasing the breaker would fix nothing.
"""

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Tokens live ~8h. Refreshing once inside the last 2h gives four attempts
# before expiry at a 30-minute cadence — enough to ride out a transient
# network failure without refreshing so eagerly that we churn tokens.
_SWEEP_INTERVAL_SEC = 30 * 60
_REFRESH_WINDOW_SEC = 2 * 60 * 60

# Let boot settle (DB init, cluster peer discovery) before the first sweep,
# but stay short: the whole point is recovering a provider that is already
# stuck, and an operator restarting to clear a stuck provider should not wait.
_INITIAL_DELAY_SEC = 90

_last_sweep: dict[str, Any] = {"at": None, "checked": 0, "refreshed": 0, "failed": 0, "detail": []}


def get_last_sweep() -> dict[str, Any]:
    """Most recent sweep result, for the admin surface."""
    return dict(_last_sweep)


async def _refresh_one(provider, db) -> tuple[bool, str]:
    """Refresh one provider and release its breaker. Returns (ok, note)."""
    from app.providers.claude_oauth_flow import refresh_and_persist
    from app.routing.circuit_breaker import clear_auth_failure, force_close

    try:
        await refresh_and_persist(provider, db)
    except Exception as exc:
        # A dead refresh_token (invalid_grant that peer-pull could not
        # recover) is the one case a worker cannot fix — it needs the
        # operator to re-run the auth URL flow. Say so plainly.
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"

    # This is the half that actually un-sticks the provider. Without it the
    # row has a valid token but routing still skips it for the rest of the
    # 24-hour hold-down.
    clear_auth_failure(provider.id)
    await force_close(provider.id)
    return True, "refreshed; auth-failure cleared and breaker force-closed"


async def _run_one_sweep() -> None:
    from sqlalchemy import select

    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider

    now = time.time()
    checked = refreshed = failed = 0
    detail: list[dict[str, Any]] = []

    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(Provider).where(
                Provider.provider_type == "claude-oauth",
                Provider.enabled.is_(True),
                Provider.deleted_at.is_(None),
            )
        )
        providers = list(rows.scalars().all())

        for p in providers:
            if not p.oauth_refresh_token:
                # Nothing this worker can do; the UI already shows
                # "Needs re-auth" for this state.
                continue
            checked += 1
            expires_at = p.oauth_expires_at
            # A NULL expiry means we cannot reason about staleness. Treat it
            # as due: refreshing is cheap and idempotent, and leaving it
            # alone is how a provider silently rots.
            due = expires_at is None or (expires_at - now) <= _REFRESH_WINDOW_SEC
            if not due:
                continue

            ok, note = await _refresh_one(p, db)
            if ok:
                refreshed += 1
                await db.commit()
                logger.info(
                    "claude_oauth_expiry.refreshed provider=%s name=%s %s",
                    p.id,
                    p.name,
                    note,
                )
            else:
                failed += 1
                await db.rollback()
                logger.warning(
                    "claude_oauth_expiry.refresh_failed provider=%s name=%s %s",
                    p.id,
                    p.name,
                    note,
                )
            detail.append({"provider_id": p.id, "name": p.name, "ok": ok, "note": note})

    _last_sweep.update(at=now, checked=checked, refreshed=refreshed, failed=failed, detail=detail)


async def _sweep_loop() -> None:
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval

    hb = WorkerHeartbeat(name="claude_oauth_expiry")
    register_expected_interval("claude_oauth_expiry", _SWEEP_INTERVAL_SEC)
    await asyncio.sleep(_INITIAL_DELAY_SEC)
    while True:
        try:
            await _run_one_sweep()
            await hb.tick(
                status="ok",
                note=(
                    f"checked={_last_sweep['checked']} "
                    f"refreshed={_last_sweep['refreshed']} "
                    f"failed={_last_sweep['failed']}"
                ),
            )
        except Exception as exc:
            logger.warning("claude_oauth_expiry.sweep_failed err=%r", exc)
            await hb.tick(status="error", note=str(exc)[:200])
        await asyncio.sleep(_SWEEP_INTERVAL_SEC)


_TASK: asyncio.Task | None = None


def start() -> None:
    """Spawn the monitor. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_sweep_loop(), name="claude-oauth-expiry-monitor")


def stop() -> None:
    """Cancel the monitor. Used by tests."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        _TASK.cancel()
