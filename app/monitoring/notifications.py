"""
Email alert system with throttling.
Sends HTML-formatted alerts for circuit breaker events, billing errors,
cluster node failures, and all-providers-down conditions.
"""
import asyncio
import logging
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
import smtplib

from app.config import settings

logger = logging.getLogger(__name__)

SEVERITY_COLORS = {
    "info": "#2196F3",
    "warning": "#FF9800",
    "error": "#F44336",
    "critical": "#9C27B0",
}

_throttle: dict[str, float] = {}
_THROTTLE_SEC = 900  # 15 minutes per event type


def _is_throttled(event_type: str) -> bool:
    last = _throttle.get(event_type, 0)
    if time.time() - last < _THROTTLE_SEC:
        return True
    _throttle[event_type] = time.time()
    return False


def _build_html(severity: str, subject: str, message: str, provider_id: Optional[str]) -> str:
    color = SEVERITY_COLORS.get(severity, "#607D8B")
    provider_line = f"<p><strong>Provider:</strong> {provider_id}</p>" if provider_id else ""
    return f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px;">
      <div style="border-left: 4px solid {color}; padding: 16px; background: #f9f9f9;">
        <h2 style="color: {color}; margin-top: 0;">[{severity.upper()}] {subject}</h2>
        {provider_line}
        <p>{message}</p>
        <p style="color: #888; font-size: 12px;">llm-proxy v2 — {__import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
      </div>
    </body></html>
    """


async def send_alert(
    severity: str,
    subject: str,
    message: str,
    provider_id: Optional[str] = None,
    throttle_key: Optional[str] = None,
):
    if not settings.smtp_enabled:
        return
    if not all([settings.smtp_host, settings.smtp_user, settings.smtp_pass, settings.smtp_to]):
        return

    key = throttle_key or f"{severity}:{subject}"
    if _is_throttled(key):
        return

    try:
        await asyncio.to_thread(_send_sync, severity, subject, message, provider_id)
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}")


def _send_sync(severity: str, subject: str, message: str, provider_id: Optional[str]):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[llm-proxy] {subject}"
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = settings.smtp_to

    msg.attach(MIMEText(message, "plain"))
    msg.attach(MIMEText(_build_html(severity, subject, message, provider_id), "html"))

    use_ssl = settings.smtp_port == 465
    if use_ssl:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port)
        server.starttls()

    server.login(settings.smtp_user, settings.smtp_pass)
    server.sendmail(msg["From"], [msg["To"]], msg.as_string())
    server.quit()
    logger.info(f"Alert sent: [{severity}] {subject}")


# Convenience helpers called from other modules

async def alert_circuit_open(provider_name: str, provider_id: str, failures: int):
    await send_alert(
        "error",
        f"Circuit breaker opened: {provider_name}",
        f"Provider {provider_name} has failed {failures} consecutive times. "
        f"Circuit breaker is now open. Requests will fail over to backup providers.",
        provider_id=provider_id,
        throttle_key=f"cb_open:{provider_id}",
    )


async def alert_billing_error(provider_name: str, provider_id: str, error: str):
    await send_alert(
        "critical",
        f"Billing/quota error: {provider_name}",
        f"Provider {provider_name} returned a billing or quota error:\n\n{error}\n\n"
        f"Immediate action required. Check your API key balance.",
        provider_id=provider_id,
        throttle_key=f"billing:{provider_id}",
    )


async def alert_all_providers_down():
    await send_alert(
        "critical",
        "All providers unavailable",
        "All configured LLM providers are currently unavailable. "
        "The proxy cannot serve requests. Immediate attention required.",
        throttle_key="all_down",
    )


async def alert_cluster_node_down(node_id: str, node_url: str):
    await send_alert(
        "warning",
        f"Cluster node unreachable: {node_id}",
        f"Node {node_id} ({node_url}) has not responded to heartbeats. "
        f"The cluster is operating with reduced capacity.",
        throttle_key=f"node_down:{node_id}",
    )
