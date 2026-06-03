"""v5.0.0 — compliance endpoints (spec §8.1).

Exercises the FastAPI routes in ``app/api/compliance.py`` via the underlying
handler functions; the patterns mirror other endpoint-shape tests in this
suite (no ``TestClient`` to keep them fast + deterministic).
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

# Stub litellm before any app import (mirrors test_v5_router_compliance_pre_filter.py).
_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})


from app.api.compliance import (  # noqa: E402
    admin_compliance_events,
    admin_compliance_policy_changes,
    cluster_compliance_ready,
    debug_echo,
    me_compliance,
)
from app.auth.keys import ApiKeyRecord  # noqa: E402
from app.compliance import emit_event, emit_policy_change  # noqa: E402
from app.compliance.policy import invalidate_blocklist_cache  # noqa: E402
from app.models.db import ApiKey, Base, ComplianceEvent  # noqa: E402


async def _fresh_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return Session


def _mk_record(
    *,
    key_id="key-1",
    debug_echo=False,
    blocked=None,
    allowed_paths=None,
):
    return ApiKeyRecord(
        id=key_id,
        name="test-key",
        key_type="standard",
        debug_echo_enabled=debug_echo,
        blocked_companies=blocked,
        allowed_paths=allowed_paths,
    )


def _stub_request(headers=None):
    req = types.SimpleNamespace()
    req.headers = headers or {}
    req.url = types.SimpleNamespace(path="/api/debug/echo-client")
    return req


# ── /api/debug/echo-client ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_debug_echo_403_when_not_enabled():
    Session = await _fresh_db()
    key = _mk_record(debug_echo=False)
    async with Session() as db:
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ex:
            await debug_echo(_stub_request(), db=db, key=key)
    assert ex.value.status_code == 403
    assert "debug_echo_not_enabled" in str(ex.value.detail)


@pytest.mark.asyncio
async def test_debug_echo_would_451_when_ua_matches_blocked_company():
    Session = await _fresh_db()
    invalidate_blocklist_cache(None)
    # Seed ApiKey row so get_effective_blocklist sees per-key policy.
    async with Session() as db:
        db.add(ApiKey(
            id="key-1", name="t", key_hash="h", key_prefix="p",
            blocked_companies=["anthropic"],
            debug_echo_enabled=True,
        ))
        await db.commit()
    key = _mk_record(
        key_id="key-1", debug_echo=True,
        blocked=["anthropic"],
    )
    headers = {"user-agent": "claude-cli/2.1.88 (external, cli)"}
    async with Session() as db:
        result = await debug_echo(_stub_request(headers=headers), db=db, key=key)
    invalidate_blocklist_cache(None)
    assert result["would_451"] is True
    assert result["matched_client_product"] == "claude-cli"
    assert result["matched_pattern"] == "claude-cli/"
    assert "anthropic" in result["api_key_policy"]["effective_blocked_companies"]


@pytest.mark.asyncio
async def test_debug_echo_no_match_when_ua_clean():
    Session = await _fresh_db()
    invalidate_blocklist_cache(None)
    async with Session() as db:
        db.add(ApiKey(
            id="key-1", name="t", key_hash="h", key_prefix="p",
            blocked_companies=["anthropic"],
            debug_echo_enabled=True,
        ))
        await db.commit()
    key = _mk_record(key_id="key-1", debug_echo=True, blocked=["anthropic"])
    headers = {"user-agent": "curl/8.4.0"}
    async with Session() as db:
        result = await debug_echo(_stub_request(headers=headers), db=db, key=key)
    invalidate_blocklist_cache(None)
    assert result["would_451"] is False
    assert result["matched_client_product"] is None


# ── /api/admin/cluster/compliance-ready ──────────────────────────────


@pytest.mark.asyncio
async def test_cluster_compliance_ready_shape():
    fake_admin = types.SimpleNamespace(user_id="u", username="admin", role="admin")
    with patch("app.cluster.manager.peers", {}):
        result = await cluster_compliance_ready(_=fake_admin)
    assert "ready_for_policy_change" in result
    assert "cluster_size" in result
    assert "peers" in result
    assert "quorum_size" in result
    assert "current_compliance_state_consistent" in result
    assert "active_streams_cluster_wide" in result
    assert "active_requests_cluster_wide" in result
    assert "oldest_active_request_started_at" in result


# ── /api/admin/compliance-events ─────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_events_csv_columns():
    Session = await _fresh_db()
    async with Session() as db:
        # Seed an API key + one compliance event
        db.add(ApiKey(
            id="key-1", name="t", key_hash="h", key_prefix="p",
        ))
        await db.flush()
        await emit_event(
            db, audit_id="comp_a1", api_key_id="key-1",
            event_type="model_substitution",
            reason_code="api-key-policy:blocked-company:anthropic",
            http_status=200, blocked_company="anthropic",
            requested_model="claude-haiku", served_model="gpt-4o-mini",
            served_provider_id="p-openai",
            client_user_agent="openai-python/1.0.0",
            commit=True,
        )
    fake_admin = types.SimpleNamespace(user_id="u", username="admin", role="admin")
    async with Session() as db:
        resp = await admin_compliance_events(
            api_key_id=None, event_type=None, start=None, end=None,
            format="csv", limit=10, db=db, _=fake_admin,
        )
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
    body = b"".join(chunks).decode()
    header_line = body.splitlines()[0]
    expected = ("audit_id,api_key_id,event_type,requested_at,requested_model,"
                "served_model,served_provider_id,blocked_company,reason_code,"
                "client_user_agent,http_status")
    assert header_line == expected
    # Row count = header + 1 data row
    assert len(body.strip().splitlines()) == 2


@pytest.mark.asyncio
async def test_admin_events_json_shape():
    Session = await _fresh_db()
    async with Session() as db:
        db.add(ApiKey(id="key-1", name="t", key_hash="h", key_prefix="p"))
        await db.flush()
        await emit_event(
            db, audit_id="comp_x", api_key_id="key-1",
            event_type="model_substitution",
            reason_code="api-key-policy:blocked-company:anthropic",
            http_status=200, commit=True,
        )
    fake_admin = types.SimpleNamespace(user_id="u", username="admin", role="admin")
    async with Session() as db:
        resp = await admin_compliance_events(
            api_key_id="key-1", event_type=None, start=None, end=None,
            format="json", limit=10, db=db, _=fake_admin,
        )
    assert "events" in resp
    assert len(resp["events"]) == 1
    assert resp["events"][0]["audit_id"] == "comp_x"


# ── /api/me/compliance ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_me_compliance_returns_effective_blocklist():
    Session = await _fresh_db()
    invalidate_blocklist_cache(None)
    async with Session() as db:
        db.add(ApiKey(
            id="key-1", name="t", key_hash="h", key_prefix="p",
            blocked_companies=["anthropic"],
            allowed_paths=["/v1/chat/completions"],
        ))
        await db.flush()
        # Seed a 451 event in the 24h window
        await emit_event(
            db, audit_id="comp_z", api_key_id="key-1",
            event_type="client_product_refusal",
            reason_code="client-product-banned",
            http_status=451, commit=True,
        )
    key = _mk_record(key_id="key-1", blocked=["anthropic"],
                     allowed_paths=["/v1/chat/completions"])
    async with Session() as db:
        result = await me_compliance(db=db, key=key)
    invalidate_blocklist_cache(None)
    assert "anthropic" in result["effective_blocked_companies"]
    assert result["per_key_blocked_companies"] == ["anthropic"]
    assert result["allowed_paths"] == ["/v1/chat/completions"]
    assert result["recent_451_count_24h"] == 1
    assert result["compliance_disclaimer_url"].startswith("http")
