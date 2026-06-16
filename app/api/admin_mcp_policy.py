"""v5.7.4 — Admin endpoints to edit per-key MCP policy.

Three actions:
- GET    /api/admin/mcp/keys/{key_id}/policy
- PUT    /api/admin/mcp/keys/{key_id}/policy   (write allow/deny/budget)
- DELETE /api/admin/mcp/keys/{key_id}/policy   (clear → permissive)

All gated by ``require_admin``. Each write emits a
``CompliancePolicyChange`` audit row (policy_change_id, before/after,
actor, reason). Mirrors the v5.2.x policy-edit audit story.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db

router = APIRouter(prefix="/api/admin/mcp/keys", tags=["admin", "mcp"])


class McpPolicyResponse(BaseModel):
    api_key_id: str
    mcp_tools_allow: Optional[list[str]] = None
    mcp_tools_deny: Optional[list[str]] = None
    mcp_schema_token_budget: Optional[int] = None


class McpPolicyPutBody(BaseModel):
    mcp_tools_allow: Optional[list[str]] = Field(
        None,
        description=(
            "List of fnmatch globs (e.g. ['read_*', 'fetch_*']) that "
            "name the tools this key may use. NULL = all allowed. "
            "[] = no tools allowed. Deny list takes precedence."
        ),
    )
    mcp_tools_deny: Optional[list[str]] = Field(
        None,
        description=(
            "List of fnmatch globs that explicitly deny tools. "
            "Takes precedence over the allow list."
        ),
    )
    mcp_schema_token_budget: Optional[int] = Field(
        None,
        ge=0,
        description=(
            "Cap on cumulative tool-schema tokens returned to this "
            "key's MCP list_tools. NULL = unlimited."
        ),
    )
    reason: Optional[str] = Field(
        None,
        max_length=2000,
        description="Free-text justification captured in the audit row.",
    )


@router.get("/{key_id}/policy", response_model=McpPolicyResponse)
async def get_mcp_policy(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: AdminUser = Depends(require_admin),
) -> McpPolicyResponse:
    from app.models.db import ApiKey
    rs = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = rs.scalar_one_or_none()
    if not key:
        raise HTTPException(404, f"api_key id={key_id!r} not found")
    return McpPolicyResponse(
        api_key_id=key.id,
        mcp_tools_allow=getattr(key, "mcp_tools_allow", None),
        mcp_tools_deny=getattr(key, "mcp_tools_deny", None),
        mcp_schema_token_budget=getattr(key, "mcp_schema_token_budget", None),
    )


@router.put("/{key_id}/policy", response_model=McpPolicyResponse)
async def put_mcp_policy(
    key_id: str,
    body: McpPolicyPutBody,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
) -> McpPolicyResponse:
    from app.models.db import ApiKey, CompliancePolicyChange
    rs = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = rs.scalar_one_or_none()
    if not key:
        raise HTTPException(404, f"api_key id={key_id!r} not found")

    before = {
        "mcp_tools_allow": getattr(key, "mcp_tools_allow", None),
        "mcp_tools_deny": getattr(key, "mcp_tools_deny", None),
        "mcp_schema_token_budget": getattr(key, "mcp_schema_token_budget", None),
    }
    # Apply
    key.mcp_tools_allow = body.mcp_tools_allow
    key.mcp_tools_deny = body.mcp_tools_deny
    key.mcp_schema_token_budget = body.mcp_schema_token_budget
    after = {
        "mcp_tools_allow": body.mcp_tools_allow,
        "mcp_tools_deny": body.mcp_tools_deny,
        "mcp_schema_token_budget": body.mcp_schema_token_budget,
    }

    audit_id = f"ppc_{int(time.time()*1000):013x}{secrets.token_hex(6)}"
    db.add(CompliancePolicyChange(
        policy_change_id=audit_id,
        changed_at=datetime.utcnow(),
        changed_by_user_id=admin.username,
        scope="api_key",
        target_id=key.id,
        before_state=json.dumps(before),
        after_state=json.dumps(after),
        reason=(
            f"mcp_policy edit on key {key.name} by {admin.username}"
            + (f"; reason: {body.reason}" if body.reason else "")
        ),
        applied_to_peers=json.dumps([]),
        pending_peers=None,
        cluster_sync_status="local_only",
    ))
    await db.commit()

    return McpPolicyResponse(
        api_key_id=key.id,
        mcp_tools_allow=key.mcp_tools_allow,
        mcp_tools_deny=key.mcp_tools_deny,
        mcp_schema_token_budget=key.mcp_schema_token_budget,
    )


@router.delete("/{key_id}/policy", response_model=McpPolicyResponse)
async def clear_mcp_policy(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    admin: AdminUser = Depends(require_admin),
) -> McpPolicyResponse:
    from app.models.db import ApiKey, CompliancePolicyChange
    rs = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = rs.scalar_one_or_none()
    if not key:
        raise HTTPException(404, f"api_key id={key_id!r} not found")

    before = {
        "mcp_tools_allow": getattr(key, "mcp_tools_allow", None),
        "mcp_tools_deny": getattr(key, "mcp_tools_deny", None),
        "mcp_schema_token_budget": getattr(key, "mcp_schema_token_budget", None),
    }
    key.mcp_tools_allow = None
    key.mcp_tools_deny = None
    key.mcp_schema_token_budget = None

    audit_id = f"ppc_{int(time.time()*1000):013x}{secrets.token_hex(6)}"
    db.add(CompliancePolicyChange(
        policy_change_id=audit_id,
        changed_at=datetime.utcnow(),
        changed_by_user_id=admin.username,
        scope="api_key",
        target_id=key.id,
        before_state=json.dumps(before),
        after_state=json.dumps({
            "mcp_tools_allow": None, "mcp_tools_deny": None,
            "mcp_schema_token_budget": None,
        }),
        reason=f"mcp_policy CLEAR on key {key.name} by {admin.username}",
        applied_to_peers=json.dumps([]),
        pending_peers=None,
        cluster_sync_status="local_only",
    ))
    await db.commit()

    return McpPolicyResponse(
        api_key_id=key.id, mcp_tools_allow=None,
        mcp_tools_deny=None, mcp_schema_token_budget=None,
    )
