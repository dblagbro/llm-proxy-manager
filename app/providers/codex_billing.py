"""v3.7.27 (#245) — ChatGPT Plus / Codex Cloud usage scraper.

Captures authoritative per-account usage from the chatgpt.com analytics
page (``https://chatgpt.com/codex/cloud/settings/analytics``). Closes
the same gap as v3.7.0's Anthropic Console scraper: the proxy's local
``ProviderUsageWindow`` undercounts because the same ChatGPT Plus
subscription is used outside this proxy (mobile app, chat UI, etc.).

Key difference from the Anthropic scrape:

- The actual XHR endpoint behind the analytics page is **not yet
  documented** in this codebase. The Anthropic scrape hardcoded
  ``/api/organizations/{uuid}/usage`` because that was discovered in
  the 2026-05-10 capture. For Codex, we let the operator supply the
  endpoint URL when they paste cookies — they capture both from
  DevTools (Network panel) at the same time.

This means Phase 1 (this file) is **generic enough to support whatever
endpoint the operator captures**. Phase 2 (future) adds first-class
field extraction once the response shape is confirmed; until then we
store ``raw_response`` only on the snapshot row so the data isn't lost
when forward parsers ship.

Operator workflow:

1. Sign into chatgpt.com in a real browser.
2. Open ``https://chatgpt.com/codex/cloud/settings/analytics``.
3. Open DevTools → Network → reload the page. Look for an XHR call
   that returns the analytics JSON (typical name patterns:
   ``/api/.../usage``, ``/backend-api/.../analytics``,
   ``/codex/.../usage``).
4. Copy the full request URL.
5. DevTools → Application → Cookies → ``chatgpt.com``: copy the
   listed cookies as a JSON dict.
6. POST endpoint URL + cookies + provider id to
   ``POST /api/providers/{id}/codex-billing-credentials``.
7. The 4-hourly worker picks them up and starts collecting snapshots.

Cookies expire (typically 14-30 days). On 401/403 the scraper logs an
``auth_state=session_expired`` event so the operator knows to re-paste.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# Browser headers that mimic a real chatgpt.com session. The exact
# set of required headers will be confirmed once the operator captures
# the analytics endpoint; this is the conservative starting point
# (mirrors the Anthropic capture's approach).
_BROWSER_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "referer": "https://chatgpt.com/codex/cloud/settings/analytics",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Cookies known to be required for chatgpt.com session auth. ChatGPT
# uses ``__Secure-next-auth.session-token`` as the primary auth cookie
# under NextAuth. Cloudflare cookies appear for bot-mitigation
# challenges. The validation below is intentionally lenient — we don't
# want to block a paste that's missing one optional cookie when the
# operator might still have a valid session.
_REQUIRED_COOKIE_NAMES = ()  # nothing strictly required at parse time;
                              # scraper will surface 401/403 if auth fails
_RECOMMENDED_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "_dd_s",
    "cf_clearance",
)

DEFAULT_TIMEOUT_SEC = 30.0


@dataclass
class ScrapeResult:
    """Parsed outcome of one scrape attempt."""
    ok: bool
    http_status: Optional[int]
    auth_state: str  # "ok" | "session_expired" | "cf_blocked" | "network_error" | "config_error" | "http_error" | "parse_error"
    raw_body: Optional[str]
    parsed: Optional[dict]
    error: Optional[str]


def parse_cookie_jar(raw: str | dict) -> dict:
    """Normalize the operator's pasted cookie blob into a flat dict.

    Accepts:
      - JSON string of ``{"cookieName": "value", ...}``
      - A dict already
      - A semicolon-separated cookie header (``"name=val; name2=val2"``)

    Defensive — operator pastes will be messy.
    """
    if isinstance(raw, dict):
        return {k: str(v) for k, v in raw.items() if k and v}
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("cookies blob is empty")
    s = raw.strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
        except Exception as e:
            raise ValueError(f"cookies blob looks like JSON but didn't parse: {e}") from e
        if not isinstance(d, dict):
            raise ValueError("cookies JSON must be an object")
        return {k: str(v) for k, v in d.items() if k and v}
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


def validate_endpoint_url(url: Optional[str]) -> Optional[str]:
    """Return a human-readable reason the endpoint URL is invalid,
    or ``None`` if it looks usable."""
    if not url or not isinstance(url, str):
        return "endpoint URL is required"
    if not url.startswith("https://"):
        return "endpoint URL must be HTTPS"
    if "chatgpt.com" not in url and "openai.com" not in url:
        return "endpoint URL should be a chatgpt.com or openai.com host"
    return None


async def fetch_usage(
    *,
    endpoint_url: str,
    cookies: dict,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> ScrapeResult:
    """Make one scrape attempt against the operator-supplied analytics
    endpoint. Categorizes the outcome the same way the Anthropic
    scraper does so the worker + admin endpoint can share UI surface.

    The URL is operator-provided (captured from DevTools) — we don't
    template it, just fire a GET. If the response shape changes
    later, the field-extractor (Phase 2 of #245) is what needs an
    update; this function stays generic.
    """
    err = validate_endpoint_url(endpoint_url)
    if err:
        return ScrapeResult(
            ok=False, http_status=None, auth_state="config_error",
            raw_body=None, parsed=None, error=err,
        )
    if not cookies:
        return ScrapeResult(
            ok=False, http_status=None, auth_state="config_error",
            raw_body=None, parsed=None, error="no cookies provided",
        )
    try:
        async with httpx.AsyncClient(timeout=timeout_sec, follow_redirects=False) as client:
            resp = await client.get(endpoint_url, headers=_BROWSER_HEADERS, cookies=cookies)
    except httpx.RequestError as exc:
        return ScrapeResult(
            ok=False, http_status=None, auth_state="network_error",
            raw_body=None, parsed=None, error=f"network: {exc!s}",
        )
    body_text = resp.text
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
        # Some analytics endpoints return SSE / NDJSON / HTML; don't
        # fail the scrape just because JSON didn't parse — store the
        # raw body so the operator can inspect it.
        return ScrapeResult(
            ok=True, http_status=resp.status_code, auth_state="ok",
            raw_body=body_text, parsed=None,
            error=f"json parse failed: {exc!s}",
        )
    return ScrapeResult(
        ok=True, http_status=resp.status_code, auth_state="ok",
        raw_body=body_text, parsed=parsed_body, error=None,
    )


def parse_usage_response(body: Any) -> dict:
    """Phase 1 stub: returns an empty dict.

    The chatgpt.com analytics response shape will be discovered when
    the operator first captures a successful scrape. Until then,
    ``raw_response`` on the snapshot carries the full body for later
    extraction. Phase 2 of #245 fills this in once we know what
    ``five_hour_utilization`` / ``seven_day_utilization`` / etc map to.
    """
    return {}


async def scrape_provider_into_snapshot(db, provider) -> dict:
    """High-level helper: fetch, store, return status.

    Mirrors the shape of ``anthropic_billing.scrape_provider_into_snapshot``
    so the admin UI can render either kind of provider with a shared
    component. Stores into ``external_usage_snapshot`` with
    ``source='chatgpt_codex_v1'`` so the field is partitionable from
    the existing Anthropic data.
    """
    from app.models.db import ExternalUsageSnapshot
    if not provider.codex_usage_endpoint_url or not provider.codex_session_cookies:
        return {
            "ok": False,
            "reason": "provider has no codex billing credentials configured",
        }
    try:
        cookies = parse_cookie_jar(provider.codex_session_cookies)
    except ValueError as e:
        snap = ExternalUsageSnapshot(
            provider_id=provider.id,
            source="chatgpt_codex_v1",
            auth_state="config_error",
            error=f"cookies parse failed: {e!s}",
        )
        db.add(snap)
        await db.commit()
        return {"ok": False, "reason": str(e)}
    result = await fetch_usage(
        endpoint_url=provider.codex_usage_endpoint_url,
        cookies=cookies,
    )
    snap_kwargs: dict = {
        "provider_id": provider.id,
        "source": "chatgpt_codex_v1",
        "http_status": result.http_status,
        "auth_state": result.auth_state,
        "error": result.error,
        "raw_response": result.raw_body if result.ok else None,
    }
    # Phase 2 will populate columnar fields via parse_usage_response()
    # once the shape is known. For now ``parse_usage_response`` is a
    # stub returning {}.
    if result.ok and result.parsed is not None:
        snap_kwargs.update(parse_usage_response(result.parsed))
    snap = ExternalUsageSnapshot(**snap_kwargs)
    db.add(snap)
    await db.commit()
    if not result.ok:
        logger.warning(
            "codex_billing.scrape_failed",
            extra={
                "provider_id": provider.id,
                "auth_state": result.auth_state,
                "http_status": result.http_status,
                "error": result.error,
            },
        )
    else:
        logger.info(
            "codex_billing.snapshot_captured",
            extra={
                "provider_id": provider.id,
                "http_status": result.http_status,
                "body_bytes": len(result.raw_body or ""),
            },
        )
    return {
        "ok": result.ok,
        "auth_state": result.auth_state,
        "http_status": result.http_status,
        "snapshot_id": snap.id,
    }
