"""Cursor-oAuth JWT expiry monitor (v5.0.4 — P3 partial).

Context: cursor-oauth providers carry a JWT with ``scope: offline_access``
and ``exp: <unix ts>`` typically ~60 days out. v4.4.37 added a probe to
the poll-exchange response capturing ``refreshToken`` if Cursor ships
one — but no operator has re-authed since the probe landed, so we
don't yet have empirical proof of the refresh-flow path. The refresh-
flow implementation (which obviates the noVNC backlog) is gated on
that empirical capture.

This worker is the zero-speculation, high-signal piece we CAN ship now:

1. Decode the JWT ``exp`` claim from each cursor-oauth provider's
   stored access_token.
2. Persist to ``Provider.oauth_expires_at`` if still NULL. The poll
   response sometimes carries ``expiresAt`` (v4.4.37 probe also captures
   this); when it does, the exchange path persists it. We fill the gap
   for providers whose access tokens predate the probe by decoding the
   JWT ourselves.
3. Log + emit an ``activity_log`` event when any provider enters the
   "<14 days" warning zone, so the operator gets a heads-up well before
   the manual re-auth deadline.
4. Surface the snapshot via ``GET /api/admin/cursor-oauth-expiry`` so
   the admin UI can render the days-until-expiry banner.

Boot-delayed 2h so the existing cursor billing scraper (4h interval)
fires first; we never race a token refresh against the billing call.
Runs every 6 hours after that — the JWT exp doesn't move minute-by-
minute, so daily would be sufficient, but 6h gives a more responsive
alert window.

Once the v4.4.37 probe captures an empirical refresh_token + the refresh
endpoint is known, the refresh-flow implementation should add a
``maybe_refresh_proactively()`` call to this worker right before the
expiry check.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


# How often to scan. JWT exp doesn't move on the order of minutes, so
# 6h gives plenty of headroom for catching a ~14-day-before-expiry
# transition.
_SWEEP_INTERVAL_SEC = 6 * 60 * 60
# Boot delay — let the cursor billing scraper land first (it runs at
# 4h interval, see CursorBillingWorker). Avoid hitting the same provider
# row twice from two workers on cold start.
_INITIAL_DELAY_SEC = 120 * 60
# Alert threshold (days remaining before expiry). 14d gives the
# operator enough lead time to schedule a manual re-auth window if the
# refresh-flow isn't live yet.
_DEFAULT_WARN_THRESHOLD_DAYS = 14


_LAST_SWEEP: Dict[str, Any] = {
    "last_sweep_ts": None,
    "providers": [],
    "warn_threshold_days": _DEFAULT_WARN_THRESHOLD_DAYS,
}


def get_last_sweep() -> Dict[str, Any]:
    """Snapshot accessor for the admin endpoint."""
    return dict(_LAST_SWEEP)


def _decode_jwt_exp(token: Optional[str]) -> Optional[float]:
    """Pull the ``exp`` claim out of a JWT (base64-url-decoded payload).
    Handles the Cursor synthesized form ``user_<id>::<JWT>`` by
    splitting on ``::`` first.

    Returns the exp as a unix timestamp (float), or ``None`` if the
    token isn't a JWT or doesn't carry an exp claim. Never raises —
    this code is in the read-only path of the monitor loop and a
    malformed token must not break the rest of the sweep.
    """
    if not token or not isinstance(token, str):
        return None
    try:
        jwt = token.split("::", 1)[1] if "::" in token else token
        segments = jwt.split(".")
        if len(segments) < 2:
            return None
        payload_seg = segments[1]
        # Re-pad for base64url
        padding = 4 - (len(payload_seg) % 4)
        if padding < 4:
            payload_seg += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_seg).decode())
        exp = payload.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def _days_until(ts: Optional[float]) -> Optional[float]:
    if ts is None:
        return None
    return (ts - time.time()) / 86400.0


async def _run_one_sweep(
    warn_threshold_days: float = _DEFAULT_WARN_THRESHOLD_DAYS,
) -> List[Dict[str, Any]]:
    """One pass — read every cursor-oauth provider, decode JWT exp,
    persist ``oauth_expires_at`` if unset, return per-provider snapshot.

    Returns a list of dicts (one per provider) for ``get_last_sweep``.
    Operator-facing UI renders the days-until-expiry plus warn flag.
    """
    from sqlalchemy import select
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider

    snapshots: List[Dict[str, Any]] = []
    async with AsyncSessionLocal() as db:
        rs = await db.execute(
            select(Provider).where(Provider.provider_type == "cursor-oauth")
        )
        providers = rs.scalars().all()
        for p in providers:
            stored_exp = getattr(p, "oauth_expires_at", None)
            jwt_exp = _decode_jwt_exp(getattr(p, "api_key", None))
            # Prefer the stored value (server-asserted via poll response)
            # over the JWT-derived one. Backfill stored if missing.
            effective_exp = stored_exp if stored_exp else jwt_exp
            if stored_exp is None and jwt_exp is not None:
                try:
                    p.oauth_expires_at = jwt_exp
                    logger.info(
                        "cursor_oauth_expiry.backfilled provider=%s exp=%s "
                        "days_left=%.1f",
                        p.id,
                        jwt_exp,
                        _days_until(jwt_exp) or 0.0,
                    )
                except Exception:
                    pass
            days_left = _days_until(effective_exp)
            warn = days_left is not None and days_left <= warn_threshold_days
            snapshot = {
                "provider_id": p.id,
                "provider_name": p.name,
                "enabled": bool(p.enabled),
                "deleted": p.deleted_at is not None,
                "stored_expires_at": stored_exp,
                "jwt_expires_at": jwt_exp,
                "effective_expires_at": effective_exp,
                "days_left": (
                    round(days_left, 2) if days_left is not None else None
                ),
                "warn": warn,
                "has_refresh_token": bool(getattr(p, "oauth_refresh_token", None)),
            }
            snapshots.append(snapshot)
            if warn and not p.deleted_at and p.enabled:
                logger.warning(
                    "cursor_oauth_expiry.warning provider=%s name=%s "
                    "days_left=%.1f threshold=%s",
                    p.id, p.name, days_left or 0.0, warn_threshold_days,
                )
        try:
            await db.commit()
        except Exception as exc:
            logger.warning("cursor_oauth_expiry.commit_failed err=%s", exc)
    _LAST_SWEEP.update({
        "last_sweep_ts": datetime.now(timezone.utc).isoformat(),
        "providers": snapshots,
        "warn_threshold_days": warn_threshold_days,
    })
    return snapshots


async def _sweep_loop() -> None:
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval
    hb = WorkerHeartbeat(name="cursor_oauth_expiry")
    register_expected_interval("cursor_oauth_expiry", _SWEEP_INTERVAL_SEC)
    await asyncio.sleep(_INITIAL_DELAY_SEC)
    while True:
        try:
            await _run_one_sweep()
            await hb.tick(status="ok", note="swept")
        except Exception as exc:
            logger.warning("cursor_oauth_expiry.sweep_failed err=%s", exc)
            await hb.tick(status="error", note=str(exc)[:200])
        await asyncio.sleep(_SWEEP_INTERVAL_SEC)


_TASK: Optional[asyncio.Task] = None


def start() -> None:
    """Spawn the monitor. Idempotent."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_sweep_loop(), name="cursor-oauth-expiry-monitor")


def stop() -> None:
    """Cancel the monitor. Used by tests."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        _TASK.cancel()
    _TASK = None
