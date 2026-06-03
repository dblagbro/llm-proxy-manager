"""v4.4.41 — Cursor dashboard usage scrape.

Captures authoritative per-account usage from Cursor's billing API.
Mirrors the shape of ``anthropic_billing.py`` (v3.7.0) so the existing
``ExternalUsageSnapshot`` table, ``external_rotation`` auto-skip rule,
and ``ProvidersPage`` ``🥇 router's pick today`` badge generalize across
both vendors with no schema migration.

API discovery (2026-06-03 spike by reading upstream
``JiuZ-Chn/Cursor-To-OpenAI/routes/cursor.js`` + endpoint probing):

- **Auth shape**: ``Cookie: WorkosCursorSessionToken=<value>`` where
  ``<value>`` is the operator's ``user_<id>::<JWT>`` cookie. Our cursor-oauth
  Provider rows store this verbatim as ``api_key``, so no separate
  credential plumbing is needed (unlike Anthropic's separate cookie blob).
- **Three working endpoints** (all GET, all return JSON, all use the
  same cookie auth):

      GET https://www.cursor.com/api/auth/me
        → identity: { sub, email, name, picture, created_at, updated_at }

      GET https://www.cursor.com/api/usage-summary
        → THE ROUTING SIGNAL:
          {
            billingCycleStart, billingCycleEnd,    # ISO datetime strings
            membershipType,                        # "free" | "pro" | "business" | ...
            limitType, isUnlimited,
            individualUsage: {
              plan: {
                enabled, used, limit, remaining,
                autoPercentUsed, apiPercentUsed,
                totalPercentUsed,                  # <— our seven_day_utilization analog
                breakdown: { included, bonus, total }
              },
              onDemand: { ... }
            },
            teamUsage: { ... }
          }

      GET https://www.cursor.com/api/dashboard/get-aggregated-usage-events
        → per-modelIntent tokens + cost:
          {
            aggregations: [{ modelIntent, inputTokens, outputTokens,
                             cacheReadTokens, totalCents, tier }],
            totalInputTokens, totalOutputTokens, totalCacheReadTokens,
            totalCostCents
          }

The 500-returning POST endpoints (``/api/dashboard/get-usage`` etc) are
tRPC-style and would need body-shape reverse-engineering. NOT NEEDED for
the routing-signal use case; skip them.

Schema mapping (reusing the existing Anthropic-shaped columns):

  ExternalUsageSnapshot field      | Cursor source
  ─────────────────────────────────┼─────────────────────────────────────
  seven_day_utilization            | individualUsage.plan.totalPercentUsed
  seven_day_resets_at              | billingCycleEnd
  extra_usage_used_credits         | totalCostCents / 100 (in USD)
  extra_usage_currency             | "USD"
  raw_response                     | JSON-encoded merge of all 3 responses

The column NAMES still say ``seven_day_*`` even though Cursor's billing
window is monthly; the column LABELS are Anthropic-specific historical
artifacts but the SEMANTICS (utilization % + reset timestamp) generalize
across vendors. Renaming to ``current_utilization`` / ``period_resets_at``
would be a deferred follow-up.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# v4.4.41 live-deploy gotcha: ``www.cursor.com`` issues a 308 redirect
# to the apex ``cursor.com`` for the API endpoints. httpx's default
# redirect-follow STRIPS the Cookie header when crossing subdomain
# boundaries (security default — different from urllib). Hitting the
# apex domain directly skips the redirect and preserves the cookie.
CURSOR_DASHBOARD_HOST = "https://cursor.com"
AUTH_ME_PATH = "/api/auth/me"
USAGE_SUMMARY_PATH = "/api/usage-summary"
AGGREGATED_EVENTS_PATH = "/api/dashboard/get-aggregated-usage-events"

# Mimic a real browser (the cookie was issued during a browser session
# and Cursor's stack treats requests without a plausible UA + Accept as
# suspect). Same UA the cursor-oauth flow uses elsewhere.
_BROWSER_HEADERS = {
    "Accept": "application/json, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36"
    ),
}


@dataclass
class FetchResult:
    """Mirror of anthropic_billing.FetchResult shape so the upstream
    snapshot-writer can stay vendor-agnostic."""
    ok: bool
    http_status: Optional[int]
    auth_state: Optional[str]  # "ok" | "session_expired" | "network_error" | "parse_error"
    error: Optional[str]
    raw_body: Optional[str]    # JSON-encoded merge of all 3 endpoint bodies on success
    parsed: Optional[dict]     # the merged dict for parse_usage_response()


def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def parse_usage_response(merged: dict) -> dict:
    """Map the merged 3-endpoint response into ExternalUsageSnapshot field kwargs.

    ``merged`` shape:
      { "auth_me": {...} | None, "usage_summary": {...}, "aggregated_events": {...} }
    """
    out: dict = {}
    summary = merged.get("usage_summary") or {}
    agg = merged.get("aggregated_events") or {}

    # Routing signal: totalPercentUsed is the operator's overall
    # billing-cycle utilization. autoPercentUsed / apiPercentUsed are
    # sub-buckets; we don't surface those in snapshot columns (yet),
    # only in raw_response.
    indiv = (summary.get("individualUsage") or {}).get("plan") or {}
    if isinstance(indiv.get("totalPercentUsed"), (int, float)):
        out["seven_day_utilization"] = float(indiv["totalPercentUsed"])

    bcend = _parse_iso(summary.get("billingCycleEnd"))
    if bcend:
        out["seven_day_resets_at"] = bcend

    # Cost (in USD) and credit usage from the aggregated events
    total_cents = agg.get("totalCostCents")
    if isinstance(total_cents, (int, float)):
        out["extra_usage_used_credits"] = float(total_cents) / 100.0
        out["extra_usage_currency"] = "USD"

    return out


async def fetch_usage(*, cookie_value: str, timeout: float = 15.0) -> FetchResult:
    """Call the three working dashboard endpoints in sequence.

    ``cookie_value`` is the raw WorkosCursorSessionToken — Provider.api_key
    on a cursor-oauth row (``user_<id>::<JWT>``).

    Returns a FetchResult with all three response bodies merged into one
    dict under ``parsed``. Auth-state semantics match anthropic_billing:
      - ok: HTTP 200 on usage-summary (the authoritative routing signal)
      - session_expired: any 401/403 on any endpoint
      - network_error: httpx exceptions
      - parse_error: 200 but body not valid JSON
    """
    headers = dict(_BROWSER_HEADERS)
    headers["Cookie"] = f"WorkosCursorSessionToken={cookie_value}"

    merged: dict = {"auth_me": None, "usage_summary": None, "aggregated_events": None}
    last_status: Optional[int] = None
    auth_failed = False
    network_error: Optional[str] = None

    async def _get_one(client, path: str, key: str):
        nonlocal last_status, auth_failed, network_error
        try:
            r = await client.get(f"{CURSOR_DASHBOARD_HOST}{path}", headers=headers)
            last_status = r.status_code
            if r.status_code in (401, 403):
                auth_failed = True
                return
            if r.status_code != 200:
                # 308 / 500 etc — record status but don't fail the whole sweep
                logger.debug(
                    "cursor_billing.endpoint_non_200",
                    extra={"path": path, "status": r.status_code},
                )
                return
            try:
                merged[key] = r.json()
            except Exception as e:
                logger.debug(
                    "cursor_billing.parse_failed",
                    extra={"path": path, "err": str(e)[:200]},
                )
        except httpx.HTTPError as e:
            network_error = f"{type(e).__name__}: {e}"

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            # auth_me is informational; usage_summary is the routing
            # signal; aggregated_events adds cost detail. We fetch all
            # three serially to keep cookie-rotation behavior
            # predictable in case Cursor ever pushes a Set-Cookie.
            await _get_one(client, AUTH_ME_PATH, "auth_me")
            if not auth_failed:
                await _get_one(client, USAGE_SUMMARY_PATH, "usage_summary")
            if not auth_failed:
                await _get_one(client, AGGREGATED_EVENTS_PATH, "aggregated_events")
    except httpx.HTTPError as e:
        return FetchResult(
            ok=False, http_status=last_status,
            auth_state="network_error",
            error=f"{type(e).__name__}: {e}",
            raw_body=None, parsed=None,
        )

    if network_error:
        return FetchResult(
            ok=False, http_status=last_status,
            auth_state="network_error",
            error=network_error,
            raw_body=None, parsed=None,
        )
    if auth_failed:
        return FetchResult(
            ok=False, http_status=last_status,
            auth_state="session_expired",
            error="Cursor returned 401/403 — the stored WorkosCursorSessionToken "
                  "is no longer valid. Operator: re-authorize the provider.",
            raw_body=None, parsed=None,
        )

    # We need at LEAST usage_summary for the routing signal; the other
    # two are bonus. If usage_summary is None but no auth failure
    # occurred, the endpoint returned non-200 or unparseable JSON —
    # treat that as a partial failure but still write a row so the
    # operator sees something happened.
    if merged.get("usage_summary") is None:
        return FetchResult(
            ok=False, http_status=last_status,
            auth_state="parse_error",
            error="usage-summary endpoint returned no JSON",
            raw_body=json.dumps(merged), parsed=merged,
        )

    return FetchResult(
        ok=True, http_status=200,
        auth_state="ok",
        error=None,
        raw_body=json.dumps(merged),
        parsed=merged,
    )


async def scrape_provider_into_snapshot(db, provider) -> dict:
    """High-level helper: fetch, parse, write one ``ExternalUsageSnapshot``
    row + run the rotation evaluator. Same shape as
    ``anthropic_billing.scrape_provider_into_snapshot``."""
    from app.models.db import ExternalUsageSnapshot

    if not provider.api_key:
        return {"ok": False, "reason": "cursor-oauth provider has no api_key (stored cookie) configured"}

    result = await fetch_usage(cookie_value=provider.api_key)

    snap_kwargs: dict = {
        "provider_id": provider.id,
        "source": "cursor_dashboard_v1",
        "http_status": result.http_status,
        "auth_state": result.auth_state,
        "error": result.error,
        "raw_response": result.raw_body if result.ok else None,
    }
    if result.ok and result.parsed is not None:
        snap_kwargs.update(parse_usage_response(result.parsed))

    snap = ExternalUsageSnapshot(**snap_kwargs)
    db.add(snap)
    await db.flush()  # populate snap.id before the rotation evaluator runs

    rotation_decision: dict = {}
    if result.ok:
        try:
            from app.routing.external_rotation import evaluate_rules_for_provider
            rotation_decision = await evaluate_rules_for_provider(
                db, provider, snapshot=snap,
            )
        except Exception as exc:
            logger.warning(
                "cursor_billing.external_rotation_failed",
                extra={"provider_id": provider.id, "error": str(exc)},
            )

    await db.commit()

    if not result.ok:
        logger.warning(
            "cursor_billing.scrape_failed",
            extra={
                "provider_id": provider.id,
                "auth_state": result.auth_state,
                "http_status": result.http_status,
                "error": (result.error or "")[:200],
            },
        )
    else:
        logger.info(
            "cursor_billing.scrape_ok",
            extra={
                "provider_id": provider.id,
                "utilization_pct": snap_kwargs.get("seven_day_utilization"),
                "resets_at": str(snap_kwargs.get("seven_day_resets_at")),
                "rotation_decision": rotation_decision,
            },
        )

    return {
        "ok": result.ok,
        "auth_state": result.auth_state,
        "http_status": result.http_status,
        "rotation": rotation_decision,
        "utilization_pct": snap_kwargs.get("seven_day_utilization"),
    }
