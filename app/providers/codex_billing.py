"""v3.8.1 (#245 Phase 2) — ChatGPT Plus / Codex Cloud usage scraper.

Captures authoritative per-account usage from the chatgpt.com backend
API. Closes the same gap as v3.7.0's Anthropic Console scraper: the
proxy's local ``ProviderUsageWindow`` undercounts because the same
ChatGPT Plus subscription is used outside this proxy (mobile app, chat
UI, Codex CLI elsewhere).

Discovery (2026-05-13 dev-tools capture):

- **Endpoint**: ``GET https://chatgpt.com/backend-api/wham/usage``
- **Auth**: ``Authorization: Bearer <access_token>`` — the SAME bearer
  the inference path uses against ``/backend-api/codex/responses``
  (see app/providers/codex_oauth.py). The proxy's existing OAuth refresh
  flow (auth.openai.com/oauth/token) keeps this token valid; no extra
  operator action needed beyond the standard codex-oauth login.
- **Response shape**:
  ```json
  {
    "rate_limit": {
      "primary_window":   { "used_percent": 30, "limit_window_seconds": 18000,  "reset_at": 1778725635 },
      "secondary_window": { "used_percent": 5,  "limit_window_seconds": 604800, "reset_at": 1779312435 }
    },
    "additional_rate_limits": [
      { "limit_name": "GPT-5.3-Codex-Spark", "rate_limit": { ...same shape... } }
    ],
    "credits": { ... }, "plan_type": "prolite", ...
  }
  ```
  ``limit_window_seconds == 18000`` is the 5-hour window; ``604800`` is
  the 7-day window. We bind these to the existing
  ``five_hour_*`` / ``seven_day_*`` columns on ExternalUsageSnapshot so
  the Anthropic and ChatGPT scrapes share dashboards/routing logic.

Operator workflow: no separate paste needed. Once a codex-oauth (now
``ChatGPT-oauth-plan``) provider is configured with OAuth login, the
4h worker picks it up automatically. The legacy ``codex_session_cookies``
/ ``codex_usage_endpoint_url`` columns from v3.7.27 Phase 1 are no
longer required and remain on the schema as unused.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# Discovered endpoint — same audience as the inference path so we
# reuse the existing codex-oauth refresh flow's access_token.
USAGE_ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_TIMEOUT_SEC = 30.0


@dataclass
class ScrapeResult:
    """Parsed outcome of one scrape attempt."""
    ok: bool
    http_status: Optional[int]
    auth_state: str  # "ok" | "session_expired" | "network_error" | "http_error" | "parse_error" | "config_error"
    raw_body: Optional[str]
    parsed: Optional[dict]
    error: Optional[str]


def _epoch_to_datetime(ts: Any) -> Optional[datetime]:
    """Convert a unix epoch seconds value (int or float) to naive UTC
    datetime. Returns None on bad input — every field is defensively
    optional so a quirky response shape doesn't crash the scraper."""
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    try:
        return datetime.utcfromtimestamp(ts)
    except Exception:
        return None


def _extract_window(window: Any, expected_secs: int) -> tuple[Optional[float], Optional[datetime]]:
    """Pull (used_percent, reset_at_datetime) from one window dict.
    Validates ``limit_window_seconds`` against the expected duration
    so primary/secondary aren't accidentally swapped if upstream ever
    reorders them.
    """
    if not isinstance(window, dict):
        return None, None
    secs = window.get("limit_window_seconds")
    if secs != expected_secs:
        # Window doesn't match the expected duration; skip rather than
        # mis-bin it.
        return None, None
    pct = window.get("used_percent")
    util = float(pct) if isinstance(pct, (int, float)) else None
    resets = _epoch_to_datetime(window.get("reset_at"))
    return util, resets


def parse_usage_response(body: Any) -> dict:
    """Flatten the captured chatgpt.com ``/backend-api/wham/usage``
    response into the columnar fields on ``ExternalUsageSnapshot``.

    Both windows are checked against their expected
    ``limit_window_seconds`` (18000 = 5h, 604800 = 7d) so the binding
    is robust to upstream reordering. Defensive — missing or wrong-shape
    fields leave the corresponding columns None rather than raising.
    """
    out: dict = {}
    if not isinstance(body, dict):
        return out
    rl = body.get("rate_limit") or {}
    pw_util, pw_resets = _extract_window(rl.get("primary_window"), expected_secs=18000)
    out["five_hour_utilization"] = pw_util
    out["five_hour_resets_at"] = pw_resets
    sw_util, sw_resets = _extract_window(rl.get("secondary_window"), expected_secs=604800)
    out["seven_day_utilization"] = sw_util
    out["seven_day_resets_at"] = sw_resets
    return out


async def _fetch_with_token(access_token: str, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> tuple[int, str]:
    """Single bearer-auth GET against the wham/usage endpoint. Returns
    (http_status, body_text). No headers beyond Authorization + a polite
    User-Agent — the 2026-05-13 capture confirmed bearer-only suffices
    (OAI-Session-Id / X-OAI-IS / device headers are NOT required for
    this endpoint)."""
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        resp = await client.get(
            USAGE_ENDPOINT,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            },
        )
    return resp.status_code, resp.text


async def fetch_usage(provider, db) -> ScrapeResult:
    """Fetch one snapshot for a provider. Uses ``provider.api_key`` as
    the bearer (matches the inference path's auth posture). On 401,
    attempts ONE refresh via the existing codex_oauth_flow and retries.
    """
    if not provider or not provider.api_key:
        return ScrapeResult(
            ok=False, http_status=None, auth_state="config_error",
            raw_body=None, parsed=None,
            error="provider has no api_key (OAuth access_token)",
        )

    try:
        http_status, body_text = await _fetch_with_token(provider.api_key)
    except httpx.RequestError as exc:
        return ScrapeResult(
            ok=False, http_status=None, auth_state="network_error",
            raw_body=None, parsed=None, error=f"network: {exc!s}",
        )

    if http_status == 401 and provider.oauth_refresh_token:
        # Try one refresh, then retry the call. Mirrors the existing
        # inference-path lazy-refresh behavior in codex_oauth.py.
        logger.info(
            "codex_billing.bearer_refresh_attempt provider_id=%s",
            provider.id,
        )
        try:
            from app.providers.codex_oauth_flow import refresh_and_persist
            await refresh_and_persist(provider, db)
            # provider.api_key is now the fresh access_token (in-place mutation)
            http_status, body_text = await _fetch_with_token(provider.api_key)
        except Exception as exc:
            return ScrapeResult(
                ok=False, http_status=http_status, auth_state="session_expired",
                raw_body=body_text[:2000], parsed=None,
                error=f"refresh failed: {exc!s}",
            )

    if http_status == 401 or http_status == 403:
        return ScrapeResult(
            ok=False, http_status=http_status, auth_state="session_expired",
            raw_body=body_text[:2000], parsed=None,
            error=f"{http_status}: bearer rejected — operator may need to re-auth codex-oauth",
        )
    if http_status != 200:
        return ScrapeResult(
            ok=False, http_status=http_status, auth_state="http_error",
            raw_body=body_text[:2000], parsed=None,
            error=f"unexpected http {http_status}",
        )
    try:
        import json
        parsed = json.loads(body_text)
    except Exception as exc:
        return ScrapeResult(
            ok=False, http_status=http_status, auth_state="parse_error",
            raw_body=body_text[:2000], parsed=None,
            error=f"json parse: {exc!s}",
        )
    return ScrapeResult(
        ok=True, http_status=http_status, auth_state="ok",
        raw_body=body_text, parsed=parsed, error=None,
    )


async def scrape_provider_into_snapshot(db, provider) -> dict:
    """High-level helper: fetch, parse, persist one ExternalUsageSnapshot
    row. Mirrors anthropic_billing.scrape_provider_into_snapshot so the
    worker + admin UI share render paths.
    """
    from app.models.db import ExternalUsageSnapshot
    result = await fetch_usage(provider, db)
    snap_kwargs: dict = {
        "provider_id": provider.id,
        "source": "chatgpt_codex_v1",
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
                "five_hour_pct": snap_kwargs.get("five_hour_utilization"),
                "seven_day_pct": snap_kwargs.get("seven_day_utilization"),
            },
        )
    return {
        "ok": result.ok,
        "auth_state": result.auth_state,
        "http_status": result.http_status,
        "snapshot_id": snap.id,
        "five_hour_utilization": snap_kwargs.get("five_hour_utilization"),
        "seven_day_utilization": snap_kwargs.get("seven_day_utilization"),
    }


# v3.7.27 (Phase 1) legacy helpers kept as no-op stubs so external
# callers that imported them don't break. Their original purpose
# (operator-pasted cookies + endpoint URL) is obsolete now that we
# use the OAuth bearer.

def parse_cookie_jar(raw):
    """Phase 1 legacy stub — kept for import compat. Returns whatever
    structure parses; not used by the v3.8.1 scrape path."""
    import json
    if isinstance(raw, dict):
        return {k: str(v) for k, v in raw.items() if k and v}
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                return {k: str(v) for k, v in d.items() if k and v}
        except Exception:
            pass
    return {}


def validate_endpoint_url(url):
    """Phase 1 legacy stub. Endpoint is now hardcoded in USAGE_ENDPOINT."""
    return None
