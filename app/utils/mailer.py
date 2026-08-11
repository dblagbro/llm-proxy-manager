"""v5.22.7 — minimal SMTP sender.

Mirrors the sender that has been working in the operator's DevinGPT project
(smtplib + STARTTLS against smtpauth.earthlink.net:587, with an explicit
HELO/EHLO name because that relay requires one). Deliberately dependency-light:
no new packages, no async mail library.

Config comes from the runtime settings store (so it is editable in the admin UI)
and falls back to env-backed Pydantic settings:

    smtp_enabled, smtp_host, smtp_port, smtp_user, smtp_pass, smtp_from, smtp_helo

Sending is blocking, so callers on the event loop MUST use ``send_email_async``,
which offloads to a worker thread. A send failure NEVER propagates to the
caller's HTTP response — for password reset, telling an anonymous caller whether
mail delivery worked leaks whether the account exists.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _cfg() -> dict:
    """Runtime settings first, env/Pydantic defaults second."""
    from app.config import settings
    try:
        from app.config_runtime import get_setting
        get = get_setting
    except Exception:                                    # pragma: no cover
        get = lambda *_a, **_k: None                     # noqa: E731

    def pick(key, fallback):
        try:
            val = get(key)
        except Exception:
            val = None
        return val if val not in (None, "") else fallback

    return {
        "enabled": bool(pick("smtp_enabled", settings.smtp_enabled)),
        "host": pick("smtp_host", settings.smtp_host) or "",
        "port": int(pick("smtp_port", settings.smtp_port) or 587),
        "user": pick("smtp_user", settings.smtp_user) or "",
        "password": pick("smtp_pass", settings.smtp_pass) or "",
        "from_addr": (pick("smtp_from", settings.smtp_from)
                      or pick("smtp_user", settings.smtp_user) or ""),
        "helo": pick("smtp_helo", getattr(settings, "smtp_helo", None)) or "",
    }


def send_email(to: str, subject: str, html_body: str) -> bool:
    """Send one HTML email. Returns True on success, False on any failure.

    Never raises — callers treat mail as best-effort. Credentials are never
    logged; only the host and recipient appear in log lines.
    """
    cfg = _cfg()
    if not cfg["enabled"]:
        logger.warning("mailer.disabled to=%s subject=%r — smtp_enabled is off", to, subject)
        return False
    if not cfg["host"] or not cfg["from_addr"]:
        logger.warning("mailer.misconfigured host=%r from=%r", cfg["host"], bool(cfg["from_addr"]))
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as s:
            if cfg["helo"]:
                s.ehlo(cfg["helo"])
            s.starttls(context=ctx)
            if cfg["helo"]:
                s.ehlo(cfg["helo"])
            if cfg["user"]:
                s.login(cfg["user"], cfg["password"])
            s.sendmail(cfg["from_addr"], [to], msg.as_string())
        logger.info("mailer.sent to=%s host=%s subject=%r", to, cfg["host"], subject)
        return True
    except Exception as exc:
        # Log the TYPE and message but never the credentials.
        logger.warning("mailer.failed to=%s host=%s err=%s: %s",
                       to, cfg["host"], type(exc).__name__, str(exc)[:200])
        return False


async def send_email_async(to: str, subject: str, html_body: str) -> bool:
    """Off-thread wrapper — smtplib is blocking and would stall the event loop."""
    return await asyncio.to_thread(send_email, to, subject, html_body)


def render_password_reset_email(reset_url: str, username: str, ttl_minutes: int) -> str:
    return f"""\
<div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:28px;\
background:#1e1e2e;color:#ececf1;border-radius:12px">
  <h2 style="color:#10a37f;margin:0 0 12px">llm-proxy — Password reset</h2>
  <p style="margin:0 0 16px;color:#c8c8d4">
    A password reset was requested for <strong>{username}</strong>.
    This link expires in {ttl_minutes} minutes and can be used once.
  </p>
  <a href="{reset_url}" style="display:inline-block;background:#10a37f;color:#fff;\
padding:11px 24px;border-radius:7px;text-decoration:none;font-weight:600">Reset password</a>
  <p style="margin:20px 0 0;font-size:12px;color:#888">Or copy: {reset_url}</p>
  <p style="margin:12px 0 0;font-size:12px;color:#888">
    If you did not request this, ignore this email — your password is unchanged.
  </p>
</div>"""
