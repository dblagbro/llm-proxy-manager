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


async def airi_notify(subject: str, message: str, severity: str = "warning",
                      category: str = "monitor") -> None:
    """Send an operator notification. Never raises — a notification failure
    must not break the evaluator.

    Recipients (v4.0.3): the global alert mailbox (``settings.smtp_to``)
    always, PLUS each operator whose per-user subscription opts into this
    ``category`` at this ``severity``. Each recipient gets their own email
    with an independent throttle key."""
    try:
        from app.monitoring.notifications import send_alert
        body = f"{message}\n\nOpen AIRI to discuss or act: {_deep_link()}"
        full_subject = f"AIRI: {subject}"

        recipients: set[str] = set()
        if getattr(settings, "smtp_to", None):
            recipients.add(settings.smtp_to)
        try:
            from app.airi import notify_prefs
            from app.models.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                recipients |= await notify_prefs.resolve_recipients(
                    db, category=category, severity=severity)
        except Exception as e:
            logger.warning("airi.notify pref-resolve failed err=%r", e)

        for email in recipients:
            await send_alert(
                severity, full_subject, body, None,
                throttle_key=f"airi:{category}:{subject}:{email}", to=email,
            )
        logger.info("airi.notify sent subject=%r severity=%s category=%s recipients=%d",
                    subject, severity, category, len(recipients))
    except Exception as e:
        logger.warning("airi.notify failed subject=%r err=%r", subject, e)
