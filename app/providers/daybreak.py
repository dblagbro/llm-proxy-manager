"""OpenAI Daybreak Blue (Trusted Access for Cyber) routing policy.

Daybreak is NOT a model and NOT automatic. It is a per-request field on the
Codex Responses API — ``access_programs.cyber`` ∈ {standard, daybreak_blue,
daybreak_red} — and OpenAI defaults an approved account to ``standard`` unless
the request explicitly opts in. Verified 2026-10-30 against the live approved
account: the Codex SIWC endpoint validates the field server-side
("Supported values are: 'standard', 'daybreak_blue', and 'daybreak_red'").

The *individual* API-key path to Daybreak is withdrawn 2026-11-20; the Codex /
Sign-In-with-ChatGPT path (this proxy's ``ChatGPT-oauth-plan`` provider) is the
surviving channel. So the proxy, acting as the operator's Codex client on the
operator's own approved account, can keep Daybreak working after that date.

Policy enforced here (deliberately tight — a vetted-individual program):

  * Opt-in ONLY via the ``daybreak`` / ``daybreak-blue`` model alias. Normal
    traffic is untouched and runs at ``standard``.
  * The calling API key must carry ``daybreak_tier == "blue"`` (operator sets
    it per key). Unauthorized keys get 403 — the approval is scoped to the
    vetted account holder, not fanned out to arbitrary callers.
  * ``daybreak_red`` is HARD-BLOCKED through this proxy regardless of alias or
    key (Blue approval never implies Red; requesting it is a 403).
  * Every Daybreak-flagged request writes a ``compliance_events`` audit row so
    the operator can reconstruct exactly what ran under the approval if OpenAI
    asks.

This module is pure policy + a gate; the provider pin and model rewrite ride
the existing ``ModelAlias`` path (``daybreak`` → the approved provider,
model_id ``codex-auto-review``), and the Codex dispatch layer injects the
returned ``access_programs`` dict into the Responses body.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

# Alias names the caller may use to request Daybreak Blue.
DAYBREAK_BLUE_ALIASES = {"daybreak", "daybreak-blue", "daybreak_blue"}
# Red is recognized only so we can reject it with a clear message.
DAYBREAK_RED_ALIASES = {"daybreak-red", "daybreak_red", "daybreak-gpt-red"}

# The cyber tier value we send upstream for an authorized Blue request.
CYBER_BLUE = "daybreak_blue"


def is_daybreak_blue_request(model: Optional[str]) -> bool:
    return bool(model) and model.strip().lower() in DAYBREAK_BLUE_ALIASES


def is_daybreak_red_request(model: Optional[str]) -> bool:
    return bool(model) and model.strip().lower() in DAYBREAK_RED_ALIASES


def key_daybreak_tier(key_record) -> Optional[str]:
    """Return the key's authorized Daybreak tier ("blue") or None."""
    tier = getattr(key_record, "daybreak_tier", None)
    return tier.strip().lower() if isinstance(tier, str) and tier.strip() else None


async def daybreak_gate(
    db: AsyncSession,
    *,
    requested_model: Optional[str],
    key_record,
    alias,
    client_user_agent: Optional[str] = None,
) -> Optional[dict]:
    """Enforce Daybreak policy; return the ``access_programs`` dict to inject
    upstream, or ``None`` when the request is not a Daybreak request.

    Raises ``HTTPException`` (403/503) on a blocked or misconfigured request.
    The provider pin + model rewrite are handled by the caller via the
    ``daybreak`` ``ModelAlias`` row; this only decides *whether* the request
    may run as Daybreak Blue and records the audit trail.
    """
    # Red is blocked outright, before any other consideration.
    if is_daybreak_red_request(requested_model):
        raise HTTPException(
            403,
            "Daybreak Red is not available through this proxy. Blue approval "
            "does not grant Red access.",
        )
    if not is_daybreak_blue_request(requested_model):
        return None

    # Gate: the key must be explicitly authorized for Blue.
    if key_daybreak_tier(key_record) != "blue":
        raise HTTPException(
            403,
            "This API key is not authorized for Daybreak Blue. Daybreak is "
            "scoped to the vetted account holder; the operator must set this "
            "key's daybreak_tier to 'blue'.",
        )

    # The `daybreak` alias must exist and pin an approved provider.
    if alias is None or not getattr(alias, "provider_id", None):
        raise HTTPException(
            503,
            "Daybreak Blue is not configured: no 'daybreak' model alias pinning "
            "an approved ChatGPT-oauth-plan provider is present.",
        )

    # Audit: one compliance_events row per Daybreak-flagged request. Best
    # effort — it must never take down a legitimate request (record_outcome
    # still logs the call to activity_log regardless).
    try:
        from app.compliance.audit import emit_event, generate_audit_id
        await emit_event(
            db,
            audit_id=generate_audit_id(),
            api_key_id=getattr(key_record, "id", "unknown"),
            event_type="daybreak_access",
            reason_code=CYBER_BLUE,
            http_status=200,
            requested_model=requested_model,
            served_model=getattr(alias, "model_id", None),
            served_provider_id=getattr(alias, "provider_id", None),
            client_user_agent=client_user_agent,
            commit=False,
        )
    except Exception:
        pass

    return {"cyber": CYBER_BLUE}
