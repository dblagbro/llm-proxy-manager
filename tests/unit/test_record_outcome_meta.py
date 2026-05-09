"""Tests for ``record_outcome`` event_meta shape — focused on v3.2.12's
``api_key_prefix`` denormalization.

Pre-fix the activity log only carried ``api_key_id``; readers had to
JOIN api_keys to know which caller did the request. The 2026-05-09
proactive-monitoring sweep mis-attributed traffic because of this gap.
v3.2.12 adds ``api_key_prefix`` directly to event_meta so log greps
and dashboard filters are self-contained.
"""
from __future__ import annotations

import time

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture
async def fixture_db():
    """Bring up a fresh DB session, seed a Provider + ApiKey row, yield
    the session factory."""
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import ApiKey, Base, Provider

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ApiKey).where(ApiKey.id == "rmeta-key"))
        await cleanup.execute(delete(Provider).where(Provider.id == "rmeta-prov"))
        await cleanup.commit()
    async with AsyncSessionLocal() as db:
        db.add(Provider(
            id="rmeta-prov", name="rmeta-test",
            provider_type="claude-oauth",
            priority=10, enabled=True,
        ))
        db.add(ApiKey(
            id="rmeta-key", name="rmeta-key-name",
            key_prefix="llmp-test123",
            key_hash="hash-stub",
            enabled=True,
        ))
        await db.commit()

    yield AsyncSessionLocal

    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ApiKey).where(ApiKey.id == "rmeta-key"))
        await cleanup.execute(delete(Provider).where(Provider.id == "rmeta-prov"))
        await cleanup.commit()


@pytest.mark.asyncio
async def test_success_meta_includes_api_key_prefix(fixture_db, monkeypatch):
    """Successful record_outcome must populate event_meta.api_key_prefix
    with the caller's key prefix (denormalized from api_keys row)."""
    from app.monitoring.helpers import record_outcome

    captured = []

    async def fake_log(db, *, event_type, message, severity, provider_id, api_key_id, metadata):
        captured.append({
            "type": event_type, "severity": severity,
            "provider_id": provider_id, "api_key_id": api_key_id,
            "metadata": metadata,
        })

    monkeypatch.setattr("app.monitoring.helpers.log_event", fake_log)
    # No-op the rest of the side-effecting machinery
    async def _noop(*a, **kw): return None
    monkeypatch.setattr("app.monitoring.helpers.record_success", _noop)
    monkeypatch.setattr("app.monitoring.helpers.record_request", _noop)
    monkeypatch.setattr("app.monitoring.helpers.record_cost", _noop)
    monkeypatch.setattr("app.monitoring.helpers.observe_request", lambda **kw: None)
    monkeypatch.setattr("app.monitoring.helpers.observe_ttft", lambda *a: None)
    monkeypatch.setattr("app.monitoring.helpers.record_ttft_sample", lambda *a: None)
    monkeypatch.setattr("app.monitoring.helpers.observe_cache_tokens", lambda *a: None)
    monkeypatch.setattr("app.monitoring.helpers.clear_auth_failure", lambda *a: None)

    AsyncSessionLocal = fixture_db
    async with AsyncSessionLocal() as db:
        await record_outcome(
            db, "rmeta-prov", "claude-sonnet-4-6",
            success=True, in_tok=10, out_tok=5, t0=time.monotonic() - 0.1,
            key_record_id="rmeta-key", endpoint="messages",
        )

    assert len(captured) == 1
    meta = captured[0]["metadata"]
    assert meta["api_key_prefix"] == "llmp-test123", \
        "success path must denormalize key_prefix from api_keys row"
    assert captured[0]["api_key_id"] == "rmeta-key"


@pytest.mark.asyncio
async def test_failure_meta_includes_api_key_prefix(fixture_db, monkeypatch):
    """Same denormalization must happen on the failure path. Pre-fix
    error events were also missing the caller attribution."""
    from app.monitoring.helpers import record_outcome

    captured = []

    async def fake_log(db, *, event_type, message, severity, provider_id, api_key_id, metadata):
        captured.append({"metadata": metadata, "severity": severity})

    monkeypatch.setattr("app.monitoring.helpers.log_event", fake_log)
    async def _noop(*a, **kw): return None
    monkeypatch.setattr("app.monitoring.helpers.record_failure", _noop)
    monkeypatch.setattr("app.monitoring.helpers.record_auth_failure", _noop)
    monkeypatch.setattr("app.monitoring.helpers.record_request", _noop)
    monkeypatch.setattr("app.monitoring.helpers.observe_request", lambda **kw: None)

    AsyncSessionLocal = fixture_db
    async with AsyncSessionLocal() as db:
        await record_outcome(
            db, "rmeta-prov", "claude-sonnet-4-6",
            success=False, t0=time.monotonic() - 0.1,
            key_record_id="rmeta-key", endpoint="messages",
            error_str="some upstream 502",
        )

    assert len(captured) == 1
    assert captured[0]["severity"] == "warning"
    assert captured[0]["metadata"]["api_key_prefix"] == "llmp-test123"


@pytest.mark.asyncio
async def test_probe_keepalive_gets_literal_prefix(fixture_db, monkeypatch):
    """The magic ``probe-keepalive`` key_record_id has no row in
    api_keys; surface it as a literal prefix so probe events stay
    filterable in log greps without a special case."""
    from app.monitoring.helpers import record_outcome

    captured = []

    async def fake_log(db, *, event_type, message, severity, provider_id, api_key_id, metadata):
        captured.append({"metadata": metadata})

    monkeypatch.setattr("app.monitoring.helpers.log_event", fake_log)
    async def _noop(*a, **kw): return None
    monkeypatch.setattr("app.monitoring.helpers.record_success", _noop)
    monkeypatch.setattr("app.monitoring.helpers.record_request", _noop)
    monkeypatch.setattr("app.monitoring.helpers.record_cost", _noop)
    monkeypatch.setattr("app.monitoring.helpers.observe_request", lambda **kw: None)
    monkeypatch.setattr("app.monitoring.helpers.observe_ttft", lambda *a: None)
    monkeypatch.setattr("app.monitoring.helpers.record_ttft_sample", lambda *a: None)
    monkeypatch.setattr("app.monitoring.helpers.observe_cache_tokens", lambda *a: None)
    monkeypatch.setattr("app.monitoring.helpers.clear_auth_failure", lambda *a: None)

    AsyncSessionLocal = fixture_db
    async with AsyncSessionLocal() as db:
        await record_outcome(
            db, "rmeta-prov", "claude-sonnet-4-6",
            success=True, in_tok=1, out_tok=1, t0=time.monotonic() - 0.1,
            key_record_id="probe-keepalive",
        )

    assert captured[0]["metadata"]["api_key_prefix"] == "probe-keepalive"
    assert captured[0]["metadata"]["probe"] is True


@pytest.mark.asyncio
async def test_unknown_key_prefix_is_none(fixture_db, monkeypatch):
    """If the api_key_id doesn't resolve to a row (deleted key, stale
    reference, race), api_key_prefix is None — no crash, no fake
    attribution."""
    from app.monitoring.helpers import record_outcome

    captured = []

    async def fake_log(db, *, event_type, message, severity, provider_id, api_key_id, metadata):
        captured.append({"metadata": metadata})

    monkeypatch.setattr("app.monitoring.helpers.log_event", fake_log)
    async def _noop(*a, **kw): return None
    monkeypatch.setattr("app.monitoring.helpers.record_success", _noop)
    monkeypatch.setattr("app.monitoring.helpers.record_request", _noop)
    monkeypatch.setattr("app.monitoring.helpers.record_cost", _noop)
    monkeypatch.setattr("app.monitoring.helpers.observe_request", lambda **kw: None)
    monkeypatch.setattr("app.monitoring.helpers.observe_ttft", lambda *a: None)
    monkeypatch.setattr("app.monitoring.helpers.record_ttft_sample", lambda *a: None)
    monkeypatch.setattr("app.monitoring.helpers.observe_cache_tokens", lambda *a: None)
    monkeypatch.setattr("app.monitoring.helpers.clear_auth_failure", lambda *a: None)

    AsyncSessionLocal = fixture_db
    async with AsyncSessionLocal() as db:
        await record_outcome(
            db, "rmeta-prov", "claude-sonnet-4-6",
            success=True, in_tok=1, out_tok=1, t0=time.monotonic() - 0.1,
            key_record_id="nonexistent-key-id",
        )

    assert captured[0]["metadata"]["api_key_prefix"] is None
