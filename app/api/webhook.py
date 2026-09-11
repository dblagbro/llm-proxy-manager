"""
Webhook async delivery — M5.

Fires an HMAC-signed HTTP POST to a caller-supplied URL after a completion
finishes. Used when the request carries X-Webhook-URL.

v5.22.18 — webhooks sign with their OWN key
-------------------------------------------
This module used ``app.cluster.auth.sign_payload``, i.e. it signed
caller-facing webhooks with ``CLUSTER_SYNC_SECRET``. That is the key which
authenticates ``POST /cluster/sync``, and ``apply_sync`` can write providers,
api_keys, users and settings. So the cluster's authentication key was being
exercised, in signature form, against every caller who receives a webhook —
two trust domains sharing one secret, with the weaker one facing outward.

``WEBHOOK_SIGNING_SECRET`` is now preferred. It falls back to the cluster
secret when unset, so upgrading does not silently stop signing deliveries,
but the fallback is logged as a warning because it is the state to get out of.
"""

import hashlib
import hmac
import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_fallback_warned = False


def _signing_key() -> tuple[bytes | None, str]:
    """Return (key, source). ``key`` is None when nothing is configured.

    Mirrors ``app.cluster.auth`` in failing closed: no key, no signature, and
    therefore no delivery — an unsigned webhook tells the receiver nothing
    about who sent it.
    """
    global _fallback_warned
    dedicated = getattr(settings, "webhook_signing_secret", None)
    if dedicated:
        return dedicated.encode(), "webhook"
    fallback = settings.cluster_sync_secret
    if fallback:
        if not _fallback_warned:
            _fallback_warned = True
            logger.warning(
                "webhook.signing_key_fallback — WEBHOOK_SIGNING_SECRET is "
                "unset, so webhooks are being signed with CLUSTER_SYNC_SECRET. "
                "That key also authenticates /cluster/sync; set a dedicated "
                "webhook secret to separate the two trust domains."
            )
        return fallback.encode(), "cluster-fallback"
    return None, "unset"


def sign_webhook(body: bytes) -> str | None:
    """HMAC-SHA256 over the exact bytes to be sent. None when unconfigured."""
    key, _source = _signing_key()
    if key is None:
        return None
    return hmac.new(key, body, hashlib.sha256).hexdigest()


async def post_webhook(url: str, payload: dict) -> None:
    body = json.dumps(payload, sort_keys=True).encode()
    sig = sign_webhook(body)
    if sig is None:
        # Everything below is inside a try that swallows delivery errors
        # precisely so a webhook can never break the completion that
        # triggered it, and an unsigned webhook is not worth sending.
        logger.error(
            "webhook.skipped reason=no_signing_secret url=%s — set "
            "WEBHOOK_SIGNING_SECRET (or CLUSTER_SYNC_SECRET) to enable",
            url,
        )
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-LLM-Proxy-Sig": sig,
                },
            )
            if resp.status_code >= 400:
                logger.warning(
                    "webhook.delivery_failed",
                    extra={"url": url, "status": resp.status_code},
                )
            else:
                logger.info("webhook.delivered", extra={"url": url, "status": resp.status_code})
    except Exception as exc:
        logger.error("webhook.error", extra={"url": url, "error": str(exc)})
