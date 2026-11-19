"""Provider "needs attention" classification + guided-recovery helpers.

Logic behind the admin UI panel that lists providers whose breaker is open /
half-open / held, WHY (billing, auth, rate-limit, logged-out, unreachable) and
what the operator can do about it. ``api/`` only shapes HTTP.

Per-node: every signal here (breaker, auth-failure map, keepalive backoff) is
in-memory on THIS node. A reset here does not touch the peer; it re-probes on
its own.
"""

from __future__ import annotations

from typing import Any

REASON_BILLING = "billing"
REASON_AUTH = "auth"
REASON_RATE_LIMIT = "rate_limit"
REASON_LOGGED_OUT = "logged_out"
REASON_UNREACHABLE = "unreachable"

# Vendor billing / limits pages offered for the billing reason. Others: omitted.
BILLING_URLS: dict[str, str] = {
    "openai": "https://platform.openai.com/settings/organization/limits",
    "openrouter": "https://openrouter.ai/settings/credits",
    "anthropic": "https://console.anthropic.com/settings/billing",
}


GROK_SIGNED_OUT_MESSAGE = "Grok bridge not signed in to grok.com: click Connect Grok"


def _bridge_login_url(provider_type: str, extra_config: Any) -> str | None:
    if provider_type != "grok-web" or not isinstance(extra_config, dict):
        return None
    base = extra_config.get("bridge_url")
    if not isinstance(base, str) or not base.startswith(("http://", "https://")):
        return None
    return base.rstrip("/") + "/login"


def classify_provider(
    *,
    provider_id: str,
    provider_type: str,
    extra_config: Any,
    cb: dict | None,
    auth_failure: dict | None,
    billing_since: float | None,
    backoff: dict | None,
) -> dict | None:
    """Return an attention row, or None when the provider needs no attention.

    Precedence: billing > logged-out/auth > rate-limit > unreachable. Never
    includes credentials; ``last_error`` is the already-truncated breaker text.
    """
    state = (cb or {}).get("state", "closed")
    held = state in ("open", "half-open")
    backoff_remaining = float((backoff or {}).get("backoff_remaining_sec") or 0.0)
    if not held and not auth_failure and backoff_remaining <= 0:
        return None

    hold_remaining = float((cb or {}).get("hold_down_remaining") or 0.0)
    retry_in = hold_remaining
    row: dict = {
        "provider_id": provider_id,
        "breaker_state": state,
        "retry_in_sec": None,
        "billing_url": None,
        "bridge_login_url": None,
        "last_error": None,
        "since": None,
    }
    if auth_failure:
        row["last_error"] = auth_failure.get("last_error")
        row["since"] = auth_failure.get("since")

    if billing_since is not None and held:
        reason = REASON_BILLING
        row["billing_url"] = BILLING_URLS.get(provider_type)
        row["since"] = billing_since
    elif auth_failure:
        if provider_type == "grok-web":
            reason = REASON_LOGGED_OUT
            row["bridge_login_url"] = _bridge_login_url(provider_type, extra_config)
            row["message"] = GROK_SIGNED_OUT_MESSAGE
        else:
            reason = REASON_AUTH
    elif backoff_remaining > 0:
        reason = REASON_RATE_LIMIT
        retry_in = max(backoff_remaining, hold_remaining)
    else:
        reason = REASON_UNREACHABLE

    row["reason"] = reason
    row["retry_in_sec"] = round(retry_in, 1) if retry_in > 0 else None
    # Which inline actions the UI may offer.
    row["can_replace_key"] = reason == REASON_AUTH
    row["can_retest"] = reason in (REASON_BILLING, REASON_AUTH, REASON_UNREACHABLE)
    return row


def build_attention_list(providers: list) -> list[dict]:
    """Classify enabled providers (ORM rows) against this node's live state."""
    from app.monitoring.keepalive import get_backoff_state
    from app.routing import circuit_breaker as cb

    states = cb.get_all_states()
    auth = cb.get_all_auth_failures()
    backoff = get_backoff_state()
    out: list[dict] = []
    for p in providers:
        if not getattr(p, "enabled", True):
            continue
        row = classify_provider(
            provider_id=p.id,
            provider_type=p.provider_type,
            extra_config=getattr(p, "extra_config", None),
            cb=states.get(p.id),
            auth_failure=auth.get(p.id),
            billing_since=cb.get_billing_failure(p.id),
            backoff=backoff.get(p.id),
        )
        if row is not None:
            row["name"] = p.name
            row["provider_type"] = p.provider_type
            out.append(row)
    return out


def _test_ok(result: dict) -> bool:
    for key in ("success", "ok"):
        if key in result:
            return bool(result[key])
    return not result.get("error")


async def retest_and_reset(provider: Any) -> dict:
    """Run the provider Test; if it passed, reset the breaker on every node
    (v5.22.67; before, only the node serving the click was reset).

    Returns ``{success, reset, error?}`` -- never the key or raw test payload.
    """
    from app.providers.scanner import test_provider
    from app.cluster.breaker_reset import reset_everywhere

    try:
        result = await test_provider(provider)
    except Exception as exc:  # test must not 500 the admin UI
        return {"success": False, "reset": False, "error": str(exc)[:300]}
    if _test_ok(result):
        peers = await reset_everywhere(provider.id)
        return {"success": True, "reset": True, "peers": peers}
    return {
        "success": False,
        "reset": False,
        "error": str(result.get("error") or "test failed")[:300],
    }


async def grok_bridge_signed_in(providers: list) -> dict:
    """The operator finished Connect Grok: for each enabled grok-web provider
    whose bridge now reports logged_in, reset the breaker on every node and
    clear the keepalive chronic gate so it re-enters rotation immediately.

    Verifies against the bridge itself (the caller's claim is not trusted).
    """
    from app.cluster.breaker_reset import reset_everywhere
    from app.monitoring.keepalive import clear_chronic_gate
    from app.providers.grok_web_bridge import bridge_logged_in

    out: list[dict] = []
    seen: dict[str, bool | None] = {}
    for p in providers:
        if not getattr(p, "enabled", True) or p.provider_type != "grok-web":
            continue
        ec = getattr(p, "extra_config", None)
        ec = ec if isinstance(ec, dict) else {}
        key = str(ec.get("bridge_url"))
        if key not in seen:
            seen[key] = await bridge_logged_in(ec)
        if seen[key] is True:
            peers = await reset_everywhere(p.id)
            out.append(
                {
                    "provider_id": p.id,
                    "reset": True,
                    "gate_cleared": clear_chronic_gate(p.id),
                    "peers": peers,
                }
            )
        else:
            out.append({"provider_id": p.id, "reset": False, "logged_in": seen[key]})
    return {"providers": out}
