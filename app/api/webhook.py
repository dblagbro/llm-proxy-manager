"""
Webhook async delivery — M5.

Fires an HMAC-signed HTTP POST to a caller-supplied URL after a completion
finishes. Used when the request carries X-Webhook-URL.
"""
import json
import logging

import httpx

from app.cluster.auth import sign_payload

logger = logging.getLogger(__name__)


async def post_webhook(url: str, payload: dict) -> None:
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
