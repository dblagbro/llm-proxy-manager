"""
Webhook async delivery — M5.

Fires an HMAC-signed HTTP POST to a caller-supplied URL after a completion
finishes. Used when the request carries X-Webhook-URL.
"""
import json
import logging

import httpx

from app.cluster.auth import cluster_auth_configured, sign_payload

logger = logging.getLogger(__name__)


async def post_webhook(url: str, payload: dict) -> None:
    # v5.22.15/16 — signing fails closed when CLUSTER_SYNC_SECRET is unset.
    # Check first: everything below is inside a try that swallows delivery
    # errors precisely so a webhook can never break the completion that
    # triggered it, and an unsigned webhook is not worth sending anyway.
    if not cluster_auth_configured():
        logger.error(
            "webhook.skipped reason=no_signing_secret url=%s — refusing to "
            "send an unsigned webhook", url,
        )
        return
    body = json.dumps(payload, sort_keys=True).encode()
    sig = sign_payload(body)
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
