"""v5.20.2 — Self-update endpoint for API-key-holding integrations.

Design shape (from operator's 2026-07-05 ask):

An integrating project's AI, after negotiating an initial API key via
``/api/integration/chat``, may later need to:

1. Adjust settings the operator pre-approved for self-service edit
   (e.g., toggle refusal detection on/off as they iterate their
   client-side judge)
2. Propose new protocols / features the proxy doesn't yet support
   (e.g., "we need per-message cost budgets" or "we want to register
   a new LMRH dim for our task-specific routing")

Instead of routing every such change through the operator via memo,
this endpoint lets the caller's AI post an update request directly.
The proxy applies field updates that fall within the pre-authorized
scope and logs everything else as a protocol proposal for the
operator to review at their convenience.

Security:
- Auth via the API key itself (not the shared passphrase). Only the
  key that OWNS the row can update it.
- Fields updatable are gated by ``api_keys.self_edit_permissions``
  (JSON list). NULL = self-edit disabled entirely.
- Hard block-list of NEVER-self-editable fields overrides any
  permission list (privilege escalation prevention).
- Every attempt (allowed OR denied) written to activity_log with
  event_type ``integration.self_update`` or
  ``integration.self_update_denied``.
- Free-form ``protocol_proposal`` field is a separate log event
  (``integration.protocol_proposal``) — never mutates state, just
  gets queued for operator review.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

logger = logging.getLogger(__name__)


# Fields that ARE ever eligible for self-edit (must appear in a key's
# self_edit_permissions list to actually be editable). Kept explicit
# rather than deriving from the ORM so a new column is opt-in for
# self-edit, not automatic.
ELIGIBLE_FIELDS: dict[str, dict] = {
    "mcp_tools_allow": {"type": "list_str_or_null"},
    "mcp_tools_deny": {"type": "list_str_or_null"},
    "mcp_schema_token_budget": {"type": "int_or_null"},
    "system_prompt_mcp_augmentation": {"type": "bool"},
    "refusal_detection_enabled": {"type": "bool"},
    "refusal_prompt_hardening": {"type": "bool"},
    "refusal_retry_enabled": {"type": "bool"},
    "refusal_retry_max_attempts": {"type": "int_or_null"},
    "semantic_cache_enabled": {"type": "bool"},
}

# Fields that MUST NEVER be self-editable regardless of permission
# list. Documented here so it's obvious to future readers what
# self-update can't touch. A permission list containing any of these
# is silently ignored for those fields (not an error — that would
# leak info about what admin fields exist).
FORBIDDEN_FIELDS: frozenset[str] = frozenset({
    "id",
    "key",
    "encrypted_key",
    "key_type",
    "enabled",
    "deleted_at",
    "self_edit_permissions",  # no privilege escalation
    "spending_cap_usd",
    "daily_soft_cap_usd",
    "daily_hard_cap_usd",
    "hourly_cap_usd",
    "day_cost_usd",
    "hour_cost_usd",
    "day_bucket_ts",
    "hour_bucket_ts",
    "rate_limit_rpm",
    "rate_limit_tier",
    "blocked_companies",
    "blocked_models",
    "allowed_models",
    "allowed_paths",
    "debug_echo_enabled",
    # anything with compliance_ or oauth_ prefix, checked by name-guard below
})


router = APIRouter()


class SelfUpdateRequest(BaseModel):
    """Payload for POST /api/integration/self-update.

    Auth via the ``x-api-key`` or ``Authorization: Bearer <key>``
    header — same as any /v1/* endpoint. No passphrase; the key
    itself is the credential.

    ``updates`` is a dict of field-name → new-value. Every field
    is validated against the key's ``self_edit_permissions`` list.
    Unlisted or forbidden fields are silently ignored (not rejected
    — a partial-application response tells the caller what got
    through).

    ``protocol_proposal`` is a free-form text field. When set, the
    proposal is logged as an activity_log event with event_type
    ``integration.protocol_proposal`` — no state mutation, just
    a queued note for the operator to review.
    """
    updates: dict[str, Any] = Field(default_factory=dict)
    protocol_proposal: Optional[str] = Field(default=None, max_length=8000)
    reason: Optional[str] = Field(default=None, max_length=1000)


class SelfUpdateResult(BaseModel):
    applied: dict[str, Any]
    denied: dict[str, str]     # field -> reason
    protocol_proposal_logged: bool


def _coerce_value(field: str, value: Any) -> Any:
    """Coerce the incoming JSON value to the ORM column's expected
    type. Raises ValueError on incompatible input."""
    spec = ELIGIBLE_FIELDS.get(field)
    if spec is None:
        raise ValueError("field not eligible for self-edit")
    t = spec["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        raise ValueError("expected bool")
    if t == "int_or_null":
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("expected int or null, got bool")
        if isinstance(value, int):
            return value
        raise ValueError("expected int or null")
    if t == "list_str_or_null":
        if value is None:
            return None
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            return value
        raise ValueError("expected list[str] or null")
    raise ValueError(f"unknown coercion type: {t}")


async def _resolve_key_from_headers(
    request: Request,
    x_api_key: Optional[str],
    authorization: Optional[str],
    db,
):
    from app.models.db import ApiKey
    supplied = x_api_key
    if not supplied and authorization:
        # "Bearer <key>" shape
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            supplied = parts[1]
    if not supplied:
        raise HTTPException(status_code=401, detail="missing API key")
    rs = await db.execute(select(ApiKey).where(ApiKey.key == supplied))
    key = rs.scalar_one_or_none()
    if key is None or not key.enabled or key.deleted_at is not None:
        raise HTTPException(status_code=401, detail="invalid or disabled key")
    return key


@router.post("/api/integration/self-update", response_model=SelfUpdateResult)
async def integration_self_update(
    payload: SelfUpdateRequest,
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> SelfUpdateResult:
    """Apply caller-scoped self-updates to the API key that authorized
    the request.

    Never returns 4xx on individual field rejections — the response
    body tells the caller exactly what got applied and what was
    denied. Only auth failures / integration-disabled state produce
    4xx. This shape lets the caller's AI iterate: it can propose a
    batch of updates and see which the operator has pre-authorized
    without having to poll the admin surface.
    """
    from app.config import settings
    from app.models.database import AsyncSessionLocal
    from app.models.db import ActivityLog

    if not getattr(settings, "integration_enabled", False):
        raise HTTPException(status_code=404, detail="integration disabled")

    async with AsyncSessionLocal() as db:
        key = await _resolve_key_from_headers(request, x_api_key, authorization, db)

        permissions = list(key.self_edit_permissions or [])
        applied: dict[str, Any] = {}
        denied: dict[str, str] = {}

        for field, raw_value in payload.updates.items():
            if field in FORBIDDEN_FIELDS or field.startswith("compliance_") or field.startswith("oauth_"):
                denied[field] = "field is never self-editable (admin-only)"
                continue
            if field not in ELIGIBLE_FIELDS:
                denied[field] = "field is not on the self-edit-eligible list"
                continue
            if field not in permissions:
                denied[field] = "not in this key's self_edit_permissions"
                continue
            try:
                coerced = _coerce_value(field, raw_value)
            except ValueError as exc:
                denied[field] = f"type coercion failed: {exc}"
                continue
            setattr(key, field, coerced)
            applied[field] = coerced

        # Stamp the LWW cursor so cluster sync converges to this state.
        try:
            key.last_user_edit_at = datetime.now(timezone.utc).timestamp()
        except Exception:
            pass

        proposal_logged = False
        if payload.protocol_proposal:
            db.add(ActivityLog(
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                severity="info",
                event_type="integration.protocol_proposal",
                api_key_id=key.id,
                message=f"Protocol proposal from {key.name}: {payload.protocol_proposal[:180]}",
                event_meta={
                    "project_name": key.name,
                    "proposal": payload.protocol_proposal,
                    "reason": payload.reason,
                },
            ))
            proposal_logged = True

        # Always audit the outcome — including "no changes applied."
        db.add(ActivityLog(
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            severity="info",
            event_type=(
                "integration.self_update"
                if applied else "integration.self_update_noop"
            ),
            api_key_id=key.id,
            message=(
                f"Self-update by {key.name}: "
                f"applied={list(applied.keys())} denied={list(denied.keys())}"
            ),
            event_meta={
                "project_name": key.name,
                "applied": applied,
                "denied": denied,
                "reason": payload.reason,
                "permissions_list": permissions,
            },
        ))
        await db.commit()

    return SelfUpdateResult(
        applied=applied,
        denied=denied,
        protocol_proposal_logged=proposal_logged,
    )
