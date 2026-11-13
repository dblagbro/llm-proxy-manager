"""v5.22.59 — usage-reset advisor (ADVISOR ONLY; built but OFF by default).

Anthropic grants the operator free "limit resets" (a Fable-only reset and a
whole-week reset). The best moment to spend a Fable reset is when the weekly
limit is nearly exhausted AND a large share of it was consumed by Fable.
This module watches each claude-oauth account's usage, decides, and emails
the operator to press the button. It NEVER redeems anything: the redemption
call is unknown until captured from a real click (see ``redeem_fable_reset``).

Layout:
- ``decide()``       pure function, no I/O.
- ``check_provider`` / ``check_all_once``  fetch + decide + persist + alert.
  Shared by the in-process worker (``start()``) and the host cron script
  ``scripts/usage_reset_advisor_cron.py`` so both honour the same DB marker
  (``UsageResetAdvisorState.advised_window``).
- ``redeem_fable_reset()``  disabled stub.

Vendor parsing lives in ``app.providers.anthropic_billing.parse_fable_usage``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

NO_ACTION = "NO_ACTION"
ADVISE_FABLE_RESET = "ADVISE_FABLE_RESET"
CANNOT_DECIDE = "CANNOT_DECIDE"

WARMUP_DELAY_SEC = 120
_TASK: asyncio.Task | None = None


@dataclass(frozen=True)
class Thresholds:
    weekly_pct: float = 95.0
    fable_min_pp: float = 30.0
    skip_if_reset_within_sec: int = 7200


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str


def decide(
    weekly_pct: float | None,
    fable_pp: float | None,
    resets_available: int | None,
    secs_until_weekly_reset: float | None,
    thresholds: Thresholds = Thresholds(),
    already_advised: bool = False,
) -> Decision:
    """Pure decision. ``fable_pp`` is Fable's contribution to the WEEKLY
    limit in percentage points. ``already_advised`` means this weekly window
    has been advised for this account before (once-per-window)."""
    if weekly_pct is None:
        return Decision(CANNOT_DECIDE, "weekly utilization not available")
    if weekly_pct < thresholds.weekly_pct:
        return Decision(
            NO_ACTION,
            f"weekly {weekly_pct:.0f}% is below the {thresholds.weekly_pct:.0f}% threshold",
        )
    if resets_available is not None and resets_available <= 0:
        return Decision(NO_ACTION, "no resets available")
    if fable_pp is None:
        return Decision(
            CANNOT_DECIDE,
            f"weekly is {weekly_pct:.0f}% but Fable usage is not exposed; check manually",
        )
    if secs_until_weekly_reset is None:
        return Decision(
            CANNOT_DECIDE,
            f"weekly is {weekly_pct:.0f}% but the window reset time is unknown; check manually",
        )
    if secs_until_weekly_reset <= thresholds.skip_if_reset_within_sec:
        return Decision(
            NO_ACTION,
            f"weekly window resets on its own in {max(0, secs_until_weekly_reset) / 60:.0f} min",
        )
    if fable_pp < thresholds.fable_min_pp:
        return Decision(
            NO_ACTION,
            f"Fable is only {fable_pp:.0f} pp of the weekly limit "
            f"(need >= {thresholds.fable_min_pp:.0f})",
        )
    if already_advised:
        return Decision(NO_ACTION, "already advised for this weekly window")
    return Decision(
        ADVISE_FABLE_RESET,
        f"weekly is {weekly_pct:.0f}% and Fable used {fable_pp:.0f} pp of it; "
        f"window resets in {secs_until_weekly_reset / 3600:.1f} h. "
        "A Fable-only reset would give back that Fable share.",
    )


def window_key(resets_at: datetime | None) -> str | None:
    """Stable per-window key: resets_at (jitters by microseconds) rounded
    to the nearest hour, UTC, as ``YYYY-MM-DDTHH``."""
    if resets_at is None:
        return None
    if resets_at.tzinfo is None:
        resets_at = resets_at.replace(tzinfo=timezone.utc)  # noqa: UP017
    r = resets_at.astimezone(timezone.utc) + timedelta(minutes=30)  # noqa: UP017
    return r.strftime("%Y-%m-%dT%H")


def thresholds_from_settings() -> Thresholds:
    from app.config import settings

    return Thresholds(
        weekly_pct=float(settings.usage_reset_advisor_weekly_pct),
        fable_min_pp=float(settings.usage_reset_advisor_fable_min_pp),
        skip_if_reset_within_sec=int(settings.usage_reset_advisor_skip_if_reset_within_sec),
    )


def redeem_fable_reset(*args, **kwargs):
    """DISABLED STUB. Redeem a Fable-only limit reset.

    The endpoint MUST be captured from a real button click in the claude.ai
    UI before this is implemented; nothing in the proxy knows the request
    shape, and guessing at a state-changing claude.ai call is not allowed.
    v1 never calls this unless ``usage_reset_advisor_redeem_enabled`` is
    true, and even then it only raises.
    """
    raise NotImplementedError("Fable reset redemption endpoint not captured yet")


def _maybe_redeem(provider_id: str) -> None:
    from app.config import settings

    if not settings.usage_reset_advisor_redeem_enabled:
        return
    try:
        redeem_fable_reset()
    except NotImplementedError as e:
        logger.warning(
            "usage_reset_advisor.redeem_unimplemented provider_id=%s err=%s", provider_id, e
        )


async def _send(severity: str, subject: str, message: str, provider_id: str, key: str) -> bool:
    """Send via the proxy's email alerting. Returns False when SMTP is not
    enabled (so callers leave the marker unset and retry later)."""
    from app.config import settings
    from app.monitoring.notifications import send_alert

    if not settings.smtp_enabled:
        logger.warning("usage_reset_advisor.alert_skipped_smtp_disabled subject=%s", subject)
        return False
    await send_alert(severity, subject, message, provider_id=provider_id, throttle_key=key)
    return True


async def check_provider(db, provider, now: datetime | None = None) -> dict:
    """One check for one provider. Never raises for upstream failures."""
    from app.models.db import UsageResetAdvisorState
    from app.providers.anthropic_billing import (
        fetch_usage,
        parse_cookie_jar,
        parse_fable_usage,
    )

    now = now or datetime.now(timezone.utc)  # noqa: UP017
    naive_now = now.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: UP017
    state = await db.get(UsageResetAdvisorState, provider.id)
    if state is None:
        state = UsageResetAdvisorState(provider_id=provider.id)
        db.add(state)
    state.last_checked_at = naive_now

    unreadable: tuple[str, str] | None = None  # (dedupe key, reason)
    decision = Decision(CANNOT_DECIDE, "not evaluated")
    wkey = None
    try:
        cookies = parse_cookie_jar(provider.anthropic_session_cookies)
        res = await fetch_usage(org_uuid=provider.anthropic_org_uuid, cookies=cookies)
    except Exception as exc:  # parse errors etc. -- never echo cookie content
        res = None
        unreadable = ("fetch:config", f"credentials unusable ({type(exc).__name__})")
    if res is not None and not res.ok:
        unreadable = (f"fetch:{res.auth_state}", f"{res.auth_state}: {res.error}")
    if unreadable is None:
        info = parse_fable_usage(res.parsed)
        reset_at = info["weekly_resets_at"]
        wkey = window_key(reset_at)
        secs = None
        if reset_at is not None:
            ra = reset_at if reset_at.tzinfo else reset_at.replace(tzinfo=timezone.utc)  # noqa: UP017
            secs = (ra - now).total_seconds()
        decision = decide(
            info["weekly_pct"],
            info["fable_pp"],
            info["resets_available"],
            secs,
            thresholds_from_settings(),
            already_advised=bool(wkey and state.advised_window == wkey),
        )
        state.last_weekly_pct = info["weekly_pct"]
        state.last_fable_pp = info["fable_pp"]
        if decision.action == CANNOT_DECIDE:
            unreadable = (f"decide:{wkey}:{decision.reason[:60]}", decision.reason)

    if unreadable is not None:
        state.last_decision = CANNOT_DECIDE
        state.last_reason = unreadable[1][:500]
        if state.unreadable_alerted_key != unreadable[0]:
            sent = await _send(
                "warning",
                f"Usage reset advisor: cannot read usage for {provider.name}",
                f"Cannot read usage, check manually: {unreadable[1]}. "
                "You will not be alerted again for this same condition.",
                provider.id,
                f"reset_advisor_unreadable:{provider.id}:{unreadable[0]}",
            )
            if sent:
                state.unreadable_alerted_key = unreadable[0]
        await db.commit()
        return {
            "provider_id": provider.id,
            "action": CANNOT_DECIDE,
            "reason": unreadable[1],
            "ok": False,
        }

    state.unreadable_alerted_key = None
    state.last_decision = decision.action
    state.last_reason = decision.reason[:500]
    if decision.action == ADVISE_FABLE_RESET:
        sent = await _send(
            "critical",
            f"Spend a Fable reset now: {provider.name}",
            f"{decision.reason}\n\nPress the Fable-only reset in claude.ai "
            "(Settings > Usage). The proxy did NOT redeem anything.",
            provider.id,
            f"reset_advisor:{provider.id}:{wkey}",
        )
        if sent and wkey:
            state.advised_window = wkey
            state.advised_at = naive_now
        _maybe_redeem(provider.id)
    await db.commit()
    return {
        "provider_id": provider.id,
        "action": decision.action,
        "reason": decision.reason,
        "ok": True,
    }


async def check_all_once() -> list[dict]:
    """Check every claude-oauth provider with billing credentials."""
    from sqlalchemy import select

    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider

    out: list[dict] = []
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(Provider)
                    .where(Provider.provider_type == "claude-oauth")
                    .where(Provider.deleted_at.is_(None))
                    .where(Provider.anthropic_org_uuid.is_not(None))
                    .where(Provider.anthropic_session_cookies.is_not(None))
                )
            )
            .scalars()
            .all()
        )
        for p in rows:
            try:
                out.append(await check_provider(db, p))
            except Exception as exc:
                logger.warning(
                    "usage_reset_advisor.provider_crashed provider_id=%s err=%s",
                    p.id,
                    type(exc).__name__,
                )
                out.append(
                    {
                        "provider_id": p.id,
                        "action": CANNOT_DECIDE,
                        "reason": f"crashed: {type(exc).__name__}",
                        "ok": False,
                    }
                )
    return out


def _enabled() -> bool:
    from app.config import settings

    return bool(settings.usage_reset_advisor_enabled)


def _interval_sec() -> int:
    from app.config import settings

    return max(60, int(settings.usage_reset_advisor_interval_sec))


async def _loop() -> None:
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval

    hb = WorkerHeartbeat(name="usage_reset_advisor")
    await asyncio.sleep(WARMUP_DELAY_SEC + random.uniform(0.0, 30.0))
    while True:
        register_expected_interval("usage_reset_advisor", _interval_sec())
        if not _enabled():
            await hb.tick(status="disabled", note="usage_reset_advisor_enabled=false")
            await asyncio.sleep(300)
            continue
        try:
            results = await check_all_once()
            bad = sum(1 for r in results if not r["ok"])
            await hb.tick(
                status="ok" if not bad else "error", note=f"checked={len(results)} unreadable={bad}"
            )
        except Exception as e:
            logger.warning("usage_reset_advisor.sweep_failed err=%s", type(e).__name__)
            await hb.tick(status="error", note=type(e).__name__)
        await asyncio.sleep(_interval_sec())


def start() -> None:
    """Spawn the advisor loop. Idempotent. Loop idles while the flag is off."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop(), name="usage-reset-advisor-loop")
    logger.info("usage_reset_advisor.started enabled=%s", _enabled())
