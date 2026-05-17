"""AIRI notifier — v4.0 milestone 4.

When a monitor rule fires, or a scheduled rule acts, AIRI tells the
operator. It reuses the existing alert infrastructure
(``app/monitoring/notifications.py`` — SMTP email, throttled) rather than
building its own channel, and includes a deep link to the Routing page
so an operator on a phone can tap straight into AIRI.
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)


def _deep_link() -> str:
    """Best-effort deep link to the AIRI / Routing page."""
    base = (getattr(settings, "cluster_node_url", "") or "").rstrip("/")
    if base:
        return f"{base}/routing"
    return "the Routing page of the proxy admin UI"


async def airi_notify(subject: str, message: str, severity: str = "warning") -> None:
    """Send an operator notification. Never raises — a notification failure
    must not break the evaluator."""
    try:
        from app.monitoring.notifications import send_alert
        body = f"{message}\n\nOpen AIRI to discuss or act: {_deep_link()}"
        await send_alert(severity, f"AIRI: {subject}", body, None)
        logger.info("airi.notify sent subject=%r severity=%s", subject, severity)
    except Exception as e:
        logger.warning("airi.notify failed subject=%r err=%r", subject, e)
