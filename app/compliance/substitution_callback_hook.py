"""v5.18.0 — Outbound substitution-callback POST hook.

Closes the gap I owed hub team since 2026-06-30 (see 2026-07-02 reply
memo). v5.14 shipped the inbound registry only; this ship adds the
outbound emitter that POSTs a substitution event to the hub's
``/api/compliance/callbacks/substitution`` receiver.

Fires ONLY when ``context.substituted is True``. On every other
disposition (no policy, pass-through, or a soft substitution rejection
that didn't actually swap the model), the hook is a no-op.

Wire format — LiteLLM Python-callback keys (hub-team option #3, per
2026-07-02 reply memo):

    {
      "original_model": "<requested model>",
      "model":          "<served model>",
      "substitution":   true,
      "id":             "<compliance_events.audit_id>",
      "user_api_key_alias": "<ApiKey.label or ApiKey.name>",
      "timestamp":      1782929384.123,
      "reason":         "<substitution class>"
    }

Retries: fire-and-forget with one retry on transport failure (2s
timeout each, 1s back-off between). After both attempts, a drop is
logged to activity_log as ``substitution_callback.dropped`` — the hub
receiver's dedup key hasn't seen this event, but from the proxy's
perspective the request has already completed and we don't block on
delivery.

Auth: ``X-Proxy-Callback-Token: <shared_secret>``. Matches hub
v2.6.6 default. When ``substitution_callback_shared_secret`` is empty,
no auth header is sent (dev-mode passthrough on the hub side).

Kill switches:
- ``substitution_callback_url`` empty → hook is a no-op (default).
- ``callbacks_fail_closed=False`` in the registry → transport
  failures degrade silently instead of hitting the ``dropped`` write.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx

from app.api._response_hook_runner import HookContext

logger = logging.getLogger(__name__)

_RETRY_BACKOFF_SEC = 1.0
_HTTPX_TIMEOUT_SEC = 2.0


def _api_key_alias(key_record: Any) -> Optional[str]:
    """Preferred display alias for the API key. Matches what shows up
    in the frontend key list."""
    if key_record is None:
        return None
    for attr in ("label", "name"):
        v = getattr(key_record, attr, None)
        if v:
            return str(v)
    return None


def _build_event(context: HookContext) -> dict:
    """Serialize the LiteLLM-style event body. Called once per POST
    attempt so the timestamp is fresh."""
    body: dict = {
        "original_model": context.requested_model,
        "model": context.served_model,
        "substitution": True,
        "user_api_key_alias": _api_key_alias(context.key_record),
        "timestamp": time.time(),
    }
    if context.compliance_event_id:
        body["id"] = context.compliance_event_id
    # ``reason`` sits in context.extra so the caller can pass the
    # substitution class without inflating HookContext core fields.
    reason = None
    if context.extra:
        reason = context.extra.get("substitution_reason")
    if reason:
        body["reason"] = reason
    return body


async def _post_once(
    client: httpx.AsyncClient, url: str, body: dict, headers: dict,
) -> tuple[bool, Optional[int], Optional[str]]:
    """One POST attempt. Returns ``(ok, status_code, error_class)``.
    ``ok`` is True on any 2xx. Never raises."""
    try:
        resp = await client.post(url, json=body, headers=headers)
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        return (False, None, type(e).__name__)
    except Exception as e:
        return (False, None, type(e).__name__)
    if 200 <= resp.status_code < 300:
        return (True, resp.status_code, None)
    return (False, resp.status_code, f"http_{resp.status_code}")


async def _log_dropped_event(context: HookContext, reason: str) -> None:
    """Best-effort activity_log write when both retries fail. Owns its
    own session; never fails the hook."""
    try:
        from app.models.database import AsyncSessionLocal
        from app.models.db import ActivityLog
        async with AsyncSessionLocal() as db:
            db.add(ActivityLog(
                severity="warning",
                event_type="substitution_callback.dropped",
                api_key_id=context.api_key_id,
                provider_id=context.provider_id,
                message=(
                    f"Substitution callback POST failed after retry — "
                    f"requested={context.requested_model!r} "
                    f"served={context.served_model!r} "
                    f"reason={reason!r}"
                ),
            ))
            await db.commit()
    except Exception as e:
        logger.debug(
            "substitution_callback.dropped_log_failed err=%s", e,
        )


async def compliance_substitution_callback_hook(
    *,
    handler_id: str,
    resp_headers: dict,
    context: HookContext,
) -> Optional[dict]:
    """POST to the configured hub callback URL when a substitution
    actually happened. No-op on all other paths."""
    if not context.substituted:
        return None

    from app.config import settings
    url = getattr(settings, "substitution_callback_url", "") or ""
    if not url.strip():
        return None

    body = _build_event(context)
    headers = {"Content-Type": "application/json"}
    secret = getattr(settings, "substitution_callback_shared_secret", "") or ""
    if secret.strip():
        headers["X-Proxy-Callback-Token"] = secret

    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT_SEC) as client:
        ok, code, err = await _post_once(client, url, body, headers)
        if ok:
            # v5.19.3 — symmetric observability with hub-side v2.6.11's
            # unconditional receipt log. WARNING level so it survives
            # default INFO log filters — matches the pattern hub team
            # confirmed 2026-07-03 (2026-07-03-proxy-team-lock-retry-shipped).
            # Both sides can now grep for happy-path traversal → catch
            # one-sided drops (POST accepted at proxy but never
            # arrives at hub → network black-hole) before the soak
            # counter goes silently wrong.
            logger.warning(
                "substitution_callback.posted status=%d id=%r attempt=1",
                code, body.get("id"),
            )
            return None
        # Retry once with 1s back-off. Same body — hub dedups on ``id``.
        await asyncio.sleep(_RETRY_BACKOFF_SEC)
        ok2, code2, err2 = await _post_once(client, url, body, headers)
        if ok2:
            logger.warning(
                "substitution_callback.posted status=%d id=%r attempt=2 "
                "first_err=%r",
                code2, body.get("id"), err,
            )
            return None

    # Both attempts failed. Log the drop as an activity_log warning so
    # the operator can see this happening from the dashboard.
    reason = f"first={err} second={err2}"
    logger.warning(
        "substitution_callback.dropped reason=%r url=%s", reason, url,
    )
    # Fire-and-forget the drop-log so this hook doesn't block the
    # response return path on a DB write.
    asyncio.create_task(_log_dropped_event(context, reason))
    return None
