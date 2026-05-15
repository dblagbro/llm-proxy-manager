"""Tests for the v3.10.10 bug-log fixes.

- BUG-023 hardening: ``verify_api_key`` must reject a soft-deleted key
  even if the row is somehow left ``enabled=True`` (defence-in-depth
  against a cluster-sync merge resurrecting a tombstoned row).
- BUG-025: a malformed JSON request body must produce a clean 400, not
  a bare HTTP 500.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete


@pytest_asyncio.fixture
async def db_ready():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, ApiKey

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ApiKey).where(ApiKey.name.like("v31010-%")))
        await cleanup.commit()
    yield AsyncSessionLocal


async def _make_key(SessionLocal, *, enabled: bool, deleted: bool) -> str:
    from app.models.db import ApiKey
    from app.auth.keys import _hash_key

    raw = f"llmp-v31010-{enabled}-{deleted}-test"
    async with SessionLocal() as db:
        db.add(ApiKey(
            name=f"v31010-{enabled}-{deleted}",
            key_hash=_hash_key(raw),
            key_prefix=raw[:8],
            key_type="standard",
            enabled=enabled,
            deleted_at=datetime.now(timezone.utc) if deleted else None,
        ))
        await db.commit()
    return raw


@pytest.mark.asyncio
async def test_verify_api_key_accepts_healthy_key(db_ready):
    from app.auth.keys import verify_api_key

    raw = await _make_key(db_ready, enabled=True, deleted=False)
    async with db_ready() as db:
        rec = await verify_api_key(db, raw)
    assert rec.name == "v31010-True-False"


@pytest.mark.asyncio
async def test_verify_api_key_rejects_disabled_key(db_ready):
    from app.auth.keys import verify_api_key

    raw = await _make_key(db_ready, enabled=False, deleted=False)
    async with db_ready() as db:
        with pytest.raises(HTTPException) as exc:
            await verify_api_key(db, raw)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_api_key_rejects_soft_deleted_even_if_enabled(db_ready):
    """BUG-023 hardening — a tombstoned row that is still ``enabled``
    (e.g. resurrected by a cluster-sync merge) must not authenticate."""
    from app.auth.keys import verify_api_key

    raw = await _make_key(db_ready, enabled=True, deleted=True)
    async with db_ready() as db:
        with pytest.raises(HTTPException) as exc:
            await verify_api_key(db, raw)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_malformed_json_handler_returns_400():
    """BUG-025 — the global JSONDecodeError handler turns a malformed
    request body into a 400 with a JSON error envelope."""
    from app.main import _handle_json_decode_error

    exc = json.JSONDecodeError("Expecting value", "{not json", 0)
    resp = await _handle_json_decode_error(None, exc)
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert body["error"]["type"] == "invalid_request_error"
    assert "Malformed JSON" in body["error"]["message"]
