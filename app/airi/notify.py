"""AIRI notifier — v4.0 milestone 4.

When a monitor rule fires, or a scheduled rule acts, AIRI tells the
operator. It reuses the existing alert infrastructure
(``app/monitoring/notifications.py`` — SMTP email, throttled) rather than
building its own channel, and includes a deep link to the Routing page
so an operator on a phone can tap straight into AIRI.

v4.3.6 (BUG-031): adds a ``dry_run`` mode that resolves recipients and
renders the email body but skips the SMTP send and returns the planned
dispatch as a dict. Lets an integration test verify the full
recipient-resolution + rendering path against a live deployment without
spamming the operator's inbox. The flag can be set per-call OR via the
``AIRI_NOTIFY_DRY_RUN`` env var (truthy values: ``1``/``true``/``yes``).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def _deep_link() -> str:
    """Best-effort deep link to the AIRI / Routing page."""
    base = (getattr(settings, "cluster_node_url", "") or "").rstrip("/")
    if base:
        return f"{base}/routing"
    return "the Routing page of the proxy admin UI"


def _env_dry_run() -> bool:
    v = os.environ.get("AIRI_NOTIFY_DRY_RUN", "").strip().lower()
    return v in ("1", "true", "yes", "on")


async def airi_notify(subject: str, message: str, severity: str = "warning",
                      category: str = "monitor",
                      dry_run: bool = False) -> Optional[dict]:
    """Send an operator notification. Never raises — a notification failure
    must not break the evaluator.

    Recipients (v4.0.3): the global alert mailbox (``settings.smtp_to``)
    always, PLUS each operator whose per-user subscription opts into this
    ``category`` at this ``severity``. Each recipient gets their own email
    with an independent throttle key.

    v4.3.6: if ``dry_run`` is true (param OR ``AIRI_NOTIFY_DRY_RUN`` env
    var), the SMTP send is skipped and a dict describing the planned
    dispatch is returned — useful for live integration tests that want
    to verify recipient resolution + body rendering without spamming
    inboxes. Production callers ignore the return value (current
    behaviour), so this is non-breaking.
    """
    effective_dry_run = bool(dry_run) or _env_dry_run()
    try:
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

        if effective_dry_run:
            logger.info(
                "airi.notify DRY-RUN subject=%r severity=%s category=%s "
                "would-send-to=%d recipient(s)",
                subject, severity, category, len(recipients),
            )
            return {
                "dry_run": True,
                "subject": full_subject,
                "body": body,
                "severity": severity,
                "category": category,
                "recipients": sorted(recipients),
            }

        from app.monitoring.notifications import send_alert
        for email in recipients:
            await send_alert(
                severity, full_subject, body, None,
                throttle_key=f"airi:{category}:{subject}:{email}", to=email,
            )
        logger.info("airi.notify sent subject=%r severity=%s category=%s recipients=%d",
                    subject, severity, category, len(recipients))
        return None
    except Exception as e:
        logger.warning("airi.notify failed subject=%r err=%r", subject, e)
        return None
