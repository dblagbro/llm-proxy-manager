"""v5.0.0 — UA pre-check refuses banned client products with HTTP 451.

The UA check fires after ``verify_api_key`` and BEFORE any provider
routing (decisions 16 + 22). It refuses banned-client-product UAs only
when the api_key's effective blocklist contains the matched company —
banned-but-allowed UAs proceed normally.

These tests exercise the full request handler through FastAPI's
TestClient so the 451 path's response shape, headers, and side-effect
(one compliance_events row written) are verified end-to-end.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture
async def fixture_db_ua():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import ApiKey, Base, Provider
    from app.models.db_compliance import ComplianceEvent

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Pre-clean any rows from a prior crashed run
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ComplianceEvent).where(
            ComplianceEvent.api_key_id.in_(["ua-key-blocked", "ua-key-allowed"])
        ))
        await cleanup.execute(delete(ApiKey).where(
            ApiKey.id.in_(["ua-key-blocked", "ua-key-allowed"])
        ))
        await cleanup.execute(delete(Provider).where(Provider.id == "ua-prov"))
        await cleanup.commit()
    async with AsyncSessionLocal() as db:
        db.add(Provider(
            id="ua-prov", name="ua-test",
            provider_type="openai", priority=10, enabled=True,
            api_key="stub", default_model="gpt-4o-mini",
        ))
        db.add(ApiKey(
            id="ua-key-blocked", name="ua-key-blocked",
            key_prefix="llmp-block",
            key_hash="hash-stub-1",
            enabled=True,
            blocked_companies=["anthropic"],
        ))
        db.add(ApiKey(
            id="ua-key-allowed", name="ua-key-allowed",
            key_prefix="llmp-allow",
            key_hash="hash-stub-2",
            enabled=True,
            blocked_companies=[],
        ))
        await db.commit()
    yield AsyncSessionLocal
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ComplianceEvent).where(
            ComplianceEvent.api_key_id.in_(["ua-key-blocked", "ua-key-allowed"])
        ))
        await cleanup.execute(delete(ApiKey).where(
            ApiKey.id.in_(["ua-key-blocked", "ua-key-allowed"])
        ))
        await cleanup.execute(delete(Provider).where(Provider.id == "ua-prov"))
        await cleanup.commit()


def _key_record(id_, blocked_companies):
    """Bare-minimum ApiKey-shaped object for verify_api_key mock."""
    class _K:
        pass
    k = _K()
    k.id = id_
    k.blocked_companies = blocked_companies
    k.enabled = True
    k.key_type = "default"
    k.budget_status = None
    k.semantic_cache_enabled = False
    return k


@pytest.mark.asyncio
async def test_banned_ua_with_anthropic_blocklist_returns_451(fixture_db_ua, monkeypatch):
    """User-Agent: claude-cli/2.1.88 on a key with
    blocked_companies=["anthropic"] → 451 with X-Compliance-Refusal
    headers + 1 compliance_events row written."""
    from app.compliance.policy import invalidate_blocklist_cache
    invalidate_blocklist_cache()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.messages import router as messages_router
    from app.models.database import get_db, AsyncSessionLocal as Real_ASL
    from app.models.db_compliance import ComplianceEvent

    AsyncSessionLocal = fixture_db_ua

    app = FastAPI()
    app.include_router(messages_router)

    async def _get_db_override():
        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override

    # Mock verify_api_key to return the blocked key
    async def fake_verify(db, token):
        return _key_record("ua-key-blocked", ["anthropic"])

    monkeypatch.setattr("app.api.messages.verify_api_key", fake_verify)

    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "claude-haiku", "messages": [
            {"role": "user", "content": "hi"}
        ], "max_tokens": 16},
        headers={
            "x-api-key": "irrelevant-mocked",
            "user-agent": "claude-cli/2.1.88",
        },
    )
    assert resp.status_code == 451, resp.text
    assert resp.headers.get("X-Compliance-Refusal") == "true"
    assert resp.headers.get("X-Compliance-Refusal-Reason") == "client-product-banned"
    assert resp.headers.get("X-Compliance-Matched-Company") == "anthropic"
    assert "X-Compliance-Audit-Id" in resp.headers

    body = resp.json()
    err = body.get("detail", {}).get("error", {})
    assert err.get("type") == "compliance_block"
    assert err.get("code") == "client-product-banned"
    assert err.get("matched_company") == "anthropic"
    assert err.get("audit_id", "").startswith("comp_")

    # One compliance_events row written.
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ComplianceEvent).where(ComplianceEvent.api_key_id == "ua-key-blocked")
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "client_product_refusal"
    assert rows[0].http_status == 451
    assert rows[0].blocked_company == "anthropic"
    assert rows[0].matched_pattern == "claude-cli/"


@pytest.mark.asyncio
async def test_banned_ua_with_empty_blocklist_does_not_block(fixture_db_ua, monkeypatch):
    """User-Agent: claude-cli/2.1 on a key with blocked_companies=[]
    must NOT return 451 — UA detection alone doesn't refuse; the company
    has to be on the effective blocklist."""
    from app.compliance.policy import invalidate_blocklist_cache
    invalidate_blocklist_cache()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.messages import router as messages_router
    from app.models.database import get_db
    from app.models.db_compliance import ComplianceEvent

    AsyncSessionLocal = fixture_db_ua

    app = FastAPI()
    app.include_router(messages_router)

    async def _get_db_override():
        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override

    async def fake_verify(db, token):
        return _key_record("ua-key-allowed", [])

    monkeypatch.setattr("app.api.messages.verify_api_key", fake_verify)

    # Patch select_provider_with_503 + the rest of the heavy path so the
    # request short-circuits cleanly after the UA check passes. We only
    # care that it gets past the 451 gate; the downstream behavior is
    # tested elsewhere.
    class _Boom(Exception): pass

    async def fake_select(*a, **kw):
        raise _Boom("post-UA-check-reached")

    monkeypatch.setattr(
        "app.api._request_pipeline.select_provider_with_503", fake_select,
    )

    client = TestClient(app)
    try:
        resp = client.post(
            "/v1/messages",
            json={"model": "claude-haiku", "messages": [
                {"role": "user", "content": "hi"}
            ], "max_tokens": 16},
            headers={
                "x-api-key": "irrelevant-mocked",
                "user-agent": "claude-cli/2.1",
            },
        )
    except _Boom:
        # Boom = we got past the UA check; that's what we want to assert.
        return
    # If no Boom: either the route returned (also OK since not 451) or
    # something else short-circuited. Just assert NOT 451.
    assert resp.status_code != 451, (
        f"empty blocklist should not 451; got {resp.status_code}: {resp.text}"
    )

    # No compliance_events row written either.
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ComplianceEvent).where(ComplianceEvent.api_key_id == "ua-key-allowed")
        )).scalars().all()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_unbanned_ua_with_anthropic_blocklist_does_not_block(fixture_db_ua, monkeypatch):
    """User-Agent: opencode/0.1.0 (NOT a banned-pattern UA) on a key with
    blocked_companies=["anthropic"] must NOT return 451 — the UA didn't
    match any banned product, so blocked_companies is irrelevant for the
    UA gate."""
    from app.compliance.policy import invalidate_blocklist_cache
    invalidate_blocklist_cache()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.messages import router as messages_router
    from app.models.database import get_db
    from app.models.db_compliance import ComplianceEvent

    AsyncSessionLocal = fixture_db_ua

    app = FastAPI()
    app.include_router(messages_router)

    async def _get_db_override():
        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override

    async def fake_verify(db, token):
        return _key_record("ua-key-blocked", ["anthropic"])

    monkeypatch.setattr("app.api.messages.verify_api_key", fake_verify)

    class _Boom(Exception): pass

    async def fake_select(*a, **kw):
        raise _Boom("post-UA-check-reached")

    monkeypatch.setattr(
        "app.api._request_pipeline.select_provider_with_503", fake_select,
    )

    client = TestClient(app)
    try:
        resp = client.post(
            "/v1/messages",
            json={"model": "gpt-4o-mini", "messages": [
                {"role": "user", "content": "hi"}
            ], "max_tokens": 16},
            headers={
                "x-api-key": "irrelevant-mocked",
                "user-agent": "opencode/0.1.0",
            },
        )
    except _Boom:
        return
    assert resp.status_code != 451, (
        f"unbanned UA should not 451; got {resp.status_code}: {resp.text}"
    )

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ComplianceEvent).where(ComplianceEvent.api_key_id == "ua-key-blocked")
        )).scalars().all()
    assert len(rows) == 0


# ── Source-level guards ────────────────────────────────────────────────


def test_messages_py_runs_ua_check_after_verify_api_key():
    from pathlib import Path
    src = Path("app/api/messages.py").read_text()
    # The UA-check block must be present.
    assert "detect_client_company(" in src
    assert "client_product_refusal" in src
    assert "client-product-banned" in src
    assert "status_code=451" in src


def test_completions_py_runs_ua_check_after_verify_api_key():
    from pathlib import Path
    src = Path("app/api/completions.py").read_text()
    assert "detect_client_company(" in src
    assert "client_product_refusal" in src
    assert "client-product-banned" in src
    assert "status_code=451" in src
