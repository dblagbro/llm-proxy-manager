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
    to: Optional[str] = None,
):
    """Send an alert email. ``to`` overrides the global ``smtp_to`` recipient
    (used by AIRI per-user notification subscriptions); when None the global
    mailbox is used."""
    if not settings.smtp_enabled:
        return
    recipient = to or settings.smtp_to
    if not all([settings.smtp_host, settings.smtp_user, settings.smtp_pass]) or not recipient:
        return

    key = throttle_key or f"{severity}:{subject}"
    if _is_throttled(key):
        return

    try:
        await asyncio.to_thread(_send_sync, severity, subject, message,
                                provider_id, recipient)
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}")


def _send_sync(severity: str, subject: str, message: str,
               provider_id: Optional[str], recipient: Optional[str] = None):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[llm-proxy] {subject}"
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = recipient or settings.smtp_to

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


async def alert_high_error_rate(
    error_count: int,
    total: int,
    rate_pct: float,
    window_min: int,
    top_classes: str,
):
    """v3.10.4 — sustained error-rate alert. Fired by the observability
    sampler when severity=error requests exceed the configured rate over
    the rolling window. The throttle_key keeps a sustained incident to
    one mail per throttle window rather than one every 5-min check."""
    await send_alert(
        "error",
        f"High error rate: {rate_pct:.0f}% of requests failing",
        f"{error_count} of {total} requests in the last {window_min} min "
        f"failed with an operator-actionable error ({rate_pct:.1f}%).\n\n"
        f"Top error classes: {top_classes}\n\n"
        f"Check provider health, recent deploys, and the activity log.",
        throttle_key="high_error_rate",
    )


async def alert_cluster_node_down(node_id: str, node_url: str):
    await send_alert(
        "warning",
        f"Cluster node unreachable: {node_id}",
        f"Node {node_id} ({node_url}) has not responded to heartbeats. "
        f"The cluster is operating with reduced capacity.",
        throttle_key=f"node_down:{node_id}",
    )


async def alert_anthropic_billing_auth_expired(
    provider_name: str,
    provider_id: str,
    auth_state: str,
    consecutive_failures: int,
):
    """v3.7.7 — fire an operator alert when the Anthropic Console billing
    scraper hits a non-OK auth state for the second+ consecutive scrape.

    First failure: log only (might be a transient Cloudflare interstitial
    that clears on its own).
    Second+ consecutive failure: send email alert. Operator needs to
    re-capture cookies via the admin UI.

    Throttle key is per-provider so multiple providers can have
    independent reminders, but a single provider can't spam every
    4 hours when cookies stay expired.
    """
    state_msg = {
        "session_expired": "session cookies have expired (401/403 from Anthropic)",
        "cf_blocked": "Cloudflare is challenging the scrape (cookies stale or fingerprint changed)",
        "config_error": "billing credentials are misconfigured",
        "network_error": "network error reaching claude.ai",
        "parse_error": "Anthropic returned an unparseable response",
        "http_error": "unexpected HTTP status from Anthropic",
    }.get(auth_state, f"scrape failed with auth_state={auth_state}")
    await send_alert(
        "warning",
        f"Anthropic billing scrape failing: {provider_name}",
        (
            f"The 4-hourly billing scrape for {provider_name} has failed "
            f"{consecutive_failures} consecutive times: {state_msg}. "
            "Auto-rotation decisions for this provider will degrade to "
            "the proxy-internal slice until cookies are refreshed.\n\n"
            "Re-capture session cookies from claude.ai DevTools and paste "
            "via the Edit Provider → External Usage → Rotate cookies UI."
        ),
        provider_id=provider_id,
        throttle_key=f"billing_auth:{provider_id}",
    )
