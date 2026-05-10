"""v3.7.0 — Anthropic Console billing scraper.

Captures authoritative per-account usage from the Anthropic-side
billing API. Closes the gap where ``ProviderUsageWindow`` only sees
the proxy's slice of an Anthropic Pro Max account that's also being
used by other channels (Claude Code CLI, mobile app, etc.) —
rotation/cascade logic was triggering on a wrong signal.

Discovery (2026-05-10 capture by browser-bridge agent on the VG
account ``dblagbro@voipguru.org``):

- **Endpoint**: ``GET https://claude.ai/api/organizations/{uuid}/usage``
- **Auth**: cookie-only. The browser session's ``sessionKey`` /
  ``sessionKeyLC`` / ``routingHint`` / ``lastActiveOrg`` cookies plus
  Cloudflare's ``cf_clearance`` / ``__cf_bm`` are sufficient. No
  ``Authorization: Bearer`` header is sent.
- **Response shape**: see ``ExternalUsageSnapshot`` model docstring;
  the live capture covered ``five_hour`` / ``seven_day`` /
  per-model weekly windows / ``extra_usage`` overage block.
- **No anthropic-version / anthropic-beta** headers required (browser
  doesn't send them); standard browser headers are sufficient.

Operator workflow:

1. Sign into the Anthropic account in a real browser.
2. Open DevTools → Application → Cookies → ``https://claude.ai``,
   copy the listed cookies as a JSON dict.
3. POST that JSON + the org UUID (from the captured request URL or
   the ``lastActiveOrg`` cookie value) to
   ``POST /api/providers/{id}/anthropic-billing-credentials``.
4. The 4-hourly worker picks them up and starts collecting snapshots.

Cookies expire (typically 30+ days for ``sessionKey``). When the
scraper sees 401/403/Cloudflare-block, it logs an
``auth_state=session_expired`` event so the operator knows to re-paste.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_API_HOST = "https://claude.ai"
USAGE_PATH_TEMPLATE = "/api/organizations/{org_uuid}/usage"

# Browser headers observed in the 2026-05-10 capture. Most are
# Cloudflare/datadog noise; we send a minimal set that mimics a
# real browser without the analytics crud. If Anthropic ever
# tightens fingerprinting we'll need to expand this.
_BROWSER_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "anthropic-client-platform": "web_claude_ai",
    "anthropic-client-version": "1.0.0",
    "referer": "https://claude.ai/settings/usage",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Required cookie names for an authenticated /usage call. Missing any
# of these from the operator's paste means the capture wasn't from a
# logged-in session and the scrape will fail. Validated up-front so
# we surface a clear error instead of relying on a mystery 403.
_REQUIRED_COOKIE_NAMES = ("sessionKey",)
_RECOMMENDED_COOKIE_NAMES = (
    "sessionKey", "sessionKeyLC", "routingHint", "lastActiveOrg",
    "cf_clearance",
)

DEFAULT_TIMEOUT_SEC = 30.0


@dataclass
class ScrapeResult:
    """Parsed outcome of one scrape attempt."""
    ok: bool
    http_status: Optional[int]
    auth_state: str  # "ok" | "session_expired" | "cf_blocked" | "network_error" | "config_error"
    raw_body: Optional[str]
    parsed: Optional[dict]
    error: Optional[str]


def parse_cookie_jar(raw: str | dict) -> dict:
    """Normalize the operator's pasted cookie blob into a flat dict.

    Accepts:
      - JSON string of ``{"cookieName": "value", ...}``
      - A dict already
      - A semicolon-separated cookie header (``"name=val; name2=val2"``)

    Strips quotes around values, ignores empty entries. Defensive —
    operator pastes will be messy.
    """
    if isinstance(raw, dict):
        return {k: str(v) for k, v in raw.items() if k and v}
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("cookies blob is empty")
    s = raw.strip()
    # JSON-shaped?
    if s.startswith("{"):
        try:
            d = json.loads(s)
        except Exception as e:
            raise ValueError(f"cookies blob looks like JSON but didn't parse: {e}") from e
        if not isinstance(d, dict):
            raise ValueError("cookies JSON must be an object")
        return {k: str(v) for k, v in d.items() if k and v}
    # Cookie-header style: "name1=val1; name2=val2"
    out: dict = {}
    for chunk in s.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"')
        if k:
            out[k] = v
    if not out:
        raise ValueError("could not extract any cookies from blob")
    return out


def validate_cookies(cookies: dict) -> Optional[str]:
    """Return a human-readable reason the cookies are insufficient,
    or ``None`` if they look complete enough to attempt a scrape.
    """
    if not cookies:
        return "no cookies provided"
    missing = [c for c in _REQUIRED_COOKIE_NAMES if c not in cookies]
    if missing:
        return f"missing required cookie(s): {', '.join(missing)}"
    return None


def _parse_iso(s: Any) -> Optional[datetime]:
    """Parse an ISO8601 timestamp tolerantly. Returns None on failure
    or unexpected shapes."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_window(d: Any) -> tuple[Optional[float], Optional[datetime]]:
    """Pull (utilization, resets_at) from one of the
    ``five_hour`` / ``seven_day*`` window dicts. Returns (None, None)
    when the field is absent / null / wrong shape."""
    if not isinstance(d, dict):
        return None, None
    util = d.get("utilization")
    resets = _parse_iso(d.get("resets_at"))
    if isinstance(util, (int, float)):
        return float(util), resets
    return None, resets


def parse_usage_response(body: dict) -> dict:
    """Flatten the captured response shape into the columnar fields
    on ``ExternalUsageSnapshot``. Defensive — every field is optional;
    we never raise on missing keys, just leave them as None.

    Returns a dict the caller assigns directly to the snapshot row.
    """
    out: dict = {}
    if not isinstance(body, dict):
        return out
    five_hour_util, five_hour_resets = _extract_window(body.get("five_hour"))
    out["five_hour_utilization"] = five_hour_util
    out["five_hour_resets_at"] = five_hour_resets

    seven_day_util, seven_day_resets = _extract_window(body.get("seven_day"))
    out["seven_day_utilization"] = seven_day_util
    out["seven_day_resets_at"] = seven_day_resets

    sonnet_util, sonnet_resets = _extract_window(body.get("seven_day_sonnet"))
    out["seven_day_sonnet_utilization"] = sonnet_util
    out["seven_day_sonnet_resets_at"] = sonnet_resets

    opus_util, opus_resets = _extract_window(body.get("seven_day_opus"))
    out["seven_day_opus_utilization"] = opus_util
    out["seven_day_opus_resets_at"] = opus_resets

    eu = body.get("extra_usage")
    if isinstance(eu, dict):
        out["extra_usage_is_enabled"] = bool(eu.get("is_enabled")) if eu.get("is_enabled") is not None else None
        out["extra_usage_monthly_limit"] = eu.get("monthly_limit") if isinstance(eu.get("monthly_limit"), (int, float)) else None
        out["extra_usage_used_credits"] = eu.get("used_credits") if isinstance(eu.get("used_credits"), (int, float)) else None
        out["extra_usage_utilization"] = eu.get("utilization") if isinstance(eu.get("utilization"), (int, float)) else None
        eucur = eu.get("currency")
        out["extra_usage_currency"] = eucur if isinstance(eucur, str) else None
    return out


async def fetch_usage(
    *,
    org_uuid: str,
    cookies: dict,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> ScrapeResult:
    """Make one scrape attempt against the Anthropic Console.

    Categorizes the outcome so the caller can tell apart an expired
    session (operator action needed) vs a transient network error
    (retry next cycle) vs a successful capture.

    Cookies are passed verbatim — caller is responsible for any
    redaction in logs (this module never logs values).
    """
    cookie_err = validate_cookies(cookies)
    if cookie_err:
        return ScrapeResult(
            ok=False, http_status=None, auth_state="config_error",
            raw_body=None, parsed=None, error=cookie_err,
        )
    if not org_uuid or not isinstance(org_uuid, str):
        return ScrapeResult(
            ok=False, http_status=None, auth_state="config_error",
            raw_body=None, parsed=None, error="org_uuid is required",
        )
    url = ANTHROPIC_API_HOST + USAGE_PATH_TEMPLATE.format(org_uuid=org_uuid)
    try:
        async with httpx.AsyncClient(timeout=timeout_sec, follow_redirects=False) as client:
            resp = await client.get(url, headers=_BROWSER_HEADERS, cookies=cookies)
    except httpx.RequestError as exc:
        return ScrapeResult(
            ok=False, http_status=None, auth_state="network_error",
            raw_body=None, parsed=None, error=f"network: {exc!s}",
        )
    body_text = resp.text
    # Cloudflare interstitial is HTML, not JSON. Detect by content type
    # OR a body containing the cf-managed-challenge marker.
    if resp.status_code in (403, 503) and (
        "cf-mitigated" in resp.headers
        or "<!DOCTYPE html>" in body_text[:500]
        or "Just a moment" in body_text[:500]
    ):
        return ScrapeResult(
            ok=False, http_status=resp.status_code, auth_state="cf_blocked",
            raw_body=body_text[:2000], parsed=None,
            error="cloudflare challenge — cookies stale or fingerprint changed",
        )
    if resp.status_code in (401, 403):
        return ScrapeResult(
            ok=False, http_status=resp.status_code, auth_state="session_expired",
            raw_body=body_text[:2000], parsed=None,
            error=f"{resp.status_code}: session/auth rejected — operator must re-paste cookies",
        )
    if resp.status_code != 200:
        return ScrapeResult(
            ok=False, http_status=resp.status_code, auth_state="http_error",
            raw_body=body_text[:2000], parsed=None,
            error=f"unexpected http {resp.status_code}",
        )
    try:
        parsed_body = resp.json()
    except Exception as exc:
        return ScrapeResult(
            ok=False, http_status=resp.status_code, auth_state="parse_error",
            raw_body=body_text[:2000], parsed=None,
            error=f"json parse failed: {exc!s}",
        )
    return ScrapeResult(
        ok=True, http_status=resp.status_code, auth_state="ok",
        raw_body=body_text, parsed=parsed_body, error=None,
    )


async def scrape_provider_into_snapshot(db, provider) -> dict:
    """High-level helper: fetch, parse, and write one
    ``ExternalUsageSnapshot`` row for a Provider that has Anthropic
    Console credentials configured. Returns a small status dict for
    the caller (worker logs / admin endpoint response).
    """
    from app.models.db import ExternalUsageSnapshot
    if not provider.anthropic_org_uuid or not provider.anthropic_session_cookies:
        return {
            "ok": False,
            "reason": "provider has no anthropic billing credentials configured",
        }
    try:
        cookies = parse_cookie_jar(provider.anthropic_session_cookies)
    except ValueError as e:
        # Persist the failure so the operator sees it in /external-usage
        snap = ExternalUsageSnapshot(
            provider_id=provider.id,
            source="anthropic_console_v1",
            auth_state="config_error",
            error=f"cookies parse failed: {e!s}",
        )
        db.add(snap)
        await db.commit()
        return {"ok": False, "reason": str(e)}
    result = await fetch_usage(
        org_uuid=provider.anthropic_org_uuid, cookies=cookies,
    )
    snap_kwargs: dict = {
        "provider_id": provider.id,
        "source": "anthropic_console_v1",
        "http_status": result.http_status,
        "auth_state": result.auth_state,
        "error": result.error,
        "raw_response": result.raw_body if result.ok else None,
    }
    if result.ok and result.parsed is not None:
        snap_kwargs.update(parse_usage_response(result.parsed))
    snap = ExternalUsageSnapshot(**snap_kwargs)
    db.add(snap)
    await db.commit()
    if not result.ok:
        logger.warning(
            "anthropic_billing.scrape_failed",
            extra={
                "provider_id": provider.id,
                "auth_state": result.auth_state,
                "http_status": result.http_status,
                "error": result.error,
            },
        )
    else:
        logger.info(
            "anthropic_billing.snapshot_captured",
            extra={
                "provider_id": provider.id,
                "seven_day_pct": snap_kwargs.get("seven_day_utilization"),
                "five_hour_pct": snap_kwargs.get("five_hour_utilization"),
            },
        )
    return {
        "ok": result.ok,
        "auth_state": result.auth_state,
        "http_status": result.http_status,
        "snapshot_id": snap.id,
        "seven_day_utilization": snap_kwargs.get("seven_day_utilization"),
        "five_hour_utilization": snap_kwargs.get("five_hour_utilization"),
    }
