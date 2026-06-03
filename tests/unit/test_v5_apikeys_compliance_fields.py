"""v5.0.0 — API keys CRUD: compliance policy fields.

Verifies:
- POST persists ``blocked_companies`` / ``allowed_paths`` / ``debug_echo_enabled``
  and emits a ``compliance_policy_changes`` row.
- PATCH with unknown company ID returns 400.
- PATCH without ``reason`` when ``blocked_companies`` changes returns 422.
- ``invalidate_blocklist_cache`` is invoked after a successful policy edit.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})


from app.api.apikeys import (  # noqa: E402
    KeyCreate, KeyUpdate, create_key, update_key,
)
from app.models.db import ApiKey, Base, CompliancePolicyChange  # noqa: E402


async def _fresh_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return Session


def _admin():
    return types.SimpleNamespace(user_id="u-1", username="admin", role="admin")


# ── POST ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_persists_compliance_fields_and_emits_policy_row():
    Session = await _fresh_db()
    async with Session() as db:
        with patch(
            "app.api.apikeys.invalidate_blocklist_cache"
        ) as inv, patch(
            "app.api.apikeys._push_compliance_sync"
        ) as push:
            push.return_value = None
            body = KeyCreate(
                name="prod-key",
                blocked_companies=["anthropic"],
                allowed_paths=["/v1/chat/completions"],
                debug_echo_enabled=False,
                reason="govt-compliance ban anthropic",
            )
            result = await create_key(body=body, db=db, user=_admin())

    assert result["blocked_companies"] == ["anthropic"]
    assert result["allowed_paths"] == ["/v1/chat/completions"]
    assert result["debug_echo_enabled"] is False
    inv.assert_called_once()

    # Verify the policy-change row was committed.
    async with Session() as db:
        rs = await db.execute(select(CompliancePolicyChange))
        rows = rs.scalars().all()
    assert len(rows) == 1
    assert rows[0].scope == "per_key"
    assert rows[0].reason == "govt-compliance ban anthropic"


@pytest.mark.asyncio
async def test_post_rejects_unknown_company_id():
    Session = await _fresh_db()
    async with Session() as db:
        from fastapi import HTTPException
        body = KeyCreate(
            name="bad-key",
            blocked_companies=["unknown-co"],
            reason="test",
        )
        with pytest.raises(HTTPException) as ex:
            await create_key(body=body, db=db, user=_admin())
    assert ex.value.status_code == 400
    assert "unknown-co" in str(ex.value.detail)


@pytest.mark.asyncio
async def test_post_requires_reason_when_blocked_companies_set():
    Session = await _fresh_db()
    async with Session() as db:
        from fastapi import HTTPException
        body = KeyCreate(
            name="no-reason",
            blocked_companies=["anthropic"],
            # missing reason
        )
        with pytest.raises(HTTPException) as ex:
            await create_key(body=body, db=db, user=_admin())
    assert ex.value.status_code == 422


# ── PATCH ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_emits_policy_change_and_invalidates_cache():
    Session = await _fresh_db()
    # Seed a key with no compliance policy
    async with Session() as db:
        db.add(ApiKey(
            id="k-1", name="t", key_hash="h", key_prefix="p",
        ))
        await db.commit()

    async with Session() as db:
        with patch(
            "app.api.apikeys.invalidate_blocklist_cache"
        ) as inv, patch(
            "app.api.apikeys._push_compliance_sync"
        ):
            body = KeyUpdate(
                blocked_companies=["openai"],
                reason="block openai for sandbox A",
            )
            result = await update_key(
                key_id="k-1", body=body, db=db, user=_admin(),
            )
    assert result["blocked_companies"] == ["openai"]
    inv.assert_called_once_with("k-1")

    async with Session() as db:
        rs = await db.execute(select(CompliancePolicyChange))
        rows = rs.scalars().all()
    assert len(rows) == 1
    assert rows[0].reason == "block openai for sandbox A"
    assert rows[0].target_id == "k-1"


@pytest.mark.asyncio
async def test_patch_requires_reason_when_policy_changes():
    Session = await _fresh_db()
    async with Session() as db:
        db.add(ApiKey(id="k-1", name="t", key_hash="h", key_prefix="p"))
        await db.commit()

    async with Session() as db:
        from fastapi import HTTPException
        body = KeyUpdate(
            blocked_companies=["openai"],
            # missing reason
        )
        with pytest.raises(HTTPException) as ex:
            await update_key(
                key_id="k-1", body=body, db=db, user=_admin(),
            )
    assert ex.value.status_code == 422


@pytest.mark.asyncio
async def test_patch_rejects_unknown_company_id():
    Session = await _fresh_db()
    async with Session() as db:
        db.add(ApiKey(id="k-1", name="t", key_hash="h", key_prefix="p"))
        await db.commit()

    async with Session() as db:
        from fastapi import HTTPException
        body = KeyUpdate(
            blocked_companies=["bogus-co"],
            reason="x",
        )
        with pytest.raises(HTTPException) as ex:
            await update_key(
                key_id="k-1", body=body, db=db, user=_admin(),
            )
    assert ex.value.status_code == 400


@pytest.mark.asyncio
async def test_patch_no_policy_change_skips_audit_row():
    """PATCH that only changes ``name`` does NOT require ``reason`` and does
    NOT emit a CompliancePolicyChange."""
    Session = await _fresh_db()
    async with Session() as db:
        db.add(ApiKey(id="k-1", name="t", key_hash="h", key_prefix="p"))
        await db.commit()

    async with Session() as db:
        body = KeyUpdate(name="new-name")
        result = await update_key(
            key_id="k-1", body=body, db=db, user=_admin(),
        )
    assert result["name"] == "new-name"

    async with Session() as db:
        rs = await db.execute(select(CompliancePolicyChange))
        rows = rs.scalars().all()
    assert rows == []
