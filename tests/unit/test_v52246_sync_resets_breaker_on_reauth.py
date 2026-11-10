"""v5.22.46 (BUG-112) — a credential rotation that arrives via cluster sync
must reset the receiving node's circuit breaker.

Root cause: ``oauth-rotate`` (and background token refresh) force_close the
circuit breaker on the ORIGIN node only. That reset is in-memory / node-local
and never crosses the wire, so the peer keeps routing against an OPEN breaker
with a long exponential hold-down even though the fresh token has already been
synced into its DB — a re-authed account still looks broken on ~half of the
load-balanced traffic until the peer happens to probe-recover on its own.

Fix: ``_apply_providers`` snapshots (api_key, oauth_refresh_token,
oauth_expires_at) before the merge and, when any of them actually changed,
``force_close`` + ``clear_auth_failure`` for that provider on the local node.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture
async def fresh_db():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, Provider

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(Provider))
        await cleanup.commit()
    yield AsyncSessionLocal
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(Provider))
        await cleanup.commit()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _payload(**over):
    base = {
        "id": "pz", "name": "max-vg", "provider_type": "claude-oauth",
        "priority": 10, "enabled": True, "extra_config": {},
        "last_user_edit_at": None, "deleted_at": None,
        "timeout_sec": 30, "exclude_from_tool_requests": False,
        "hold_down_sec": None, "failure_threshold": None,
        "api_key": "tok-OLD", "oauth_refresh_token": "ref-OLD",
        "oauth_expires_at": 1000,
    }
    base.update(over)
    return base


async def _cb_state(provider_id: str) -> str:
    from app.routing.circuit_breaker import get_all_states
    return get_all_states().get(provider_id, {}).get("state")


@pytest.mark.asyncio
async def test_credential_change_closes_open_breaker(fresh_db):
    from app.cluster.sync import apply_sync
    from app.models.db import Provider
    from app.routing import circuit_breaker as cb

    now = datetime.now(timezone.utc)
    later = now + timedelta(seconds=30)
    async with fresh_db() as db:
        db.add(Provider(
            id="pz", name="max-vg", provider_type="claude-oauth",
            priority=10, enabled=True, extra_config={},
            api_key="tok-OLD", oauth_refresh_token="ref-OLD",
            oauth_expires_at=1000, updated_at=now, last_user_edit_at=100.0,
        ))
        await db.commit()

        # Simulate the peer having tripped the breaker on the stale token.
        await cb.force_open("pz")
        await cb.record_auth_failure("pz", "invalid_grant")
        assert await _cb_state("pz") == "open"

        # A re-auth on the origin node rotated all three credential columns
        # and bumped updated_at; it syncs in here.
        await apply_sync(db, {"providers": [_payload(
            api_key="tok-NEW", oauth_refresh_token="ref-NEW",
            oauth_expires_at=2000, last_user_edit_at=100.0,
            updated_at=_iso(later),
        )]})
        await db.commit()

        local = (await db.execute(
            select(Provider).where(Provider.id == "pz"))).scalar_one()
        assert local.api_key == "tok-NEW"
        assert local.oauth_refresh_token == "ref-NEW"
        assert await _cb_state("pz") == "closed", \
            "synced credential rotation must reset the peer's breaker"
        assert cb.get_auth_failure("pz") is None


@pytest.mark.asyncio
async def test_noncredential_update_leaves_breaker_untouched(fresh_db):
    """A non-credential edit (e.g. priority) that syncs in must NOT reset a
    legitimately-open breaker — we only give a fresh chance when the token
    itself changed."""
    from app.cluster.sync import apply_sync
    from app.models.db import Provider
    from app.routing import circuit_breaker as cb

    now = datetime.now(timezone.utc)
    later = now + timedelta(seconds=30)
    async with fresh_db() as db:
        db.add(Provider(
            id="pz", name="max-vg", provider_type="claude-oauth",
            priority=10, enabled=True, extra_config={},
            api_key="tok-OLD", oauth_refresh_token="ref-OLD",
            oauth_expires_at=1000, updated_at=now, last_user_edit_at=100.0,
        ))
        await db.commit()

        await cb.force_open("pz")
        assert await _cb_state("pz") == "open"

        # Same credentials, only priority changed.
        await apply_sync(db, {"providers": [_payload(
            priority=3, api_key="tok-OLD", oauth_refresh_token="ref-OLD",
            oauth_expires_at=1000, last_user_edit_at=101.0,
            updated_at=_iso(later),
        )]})
        await db.commit()

        local = (await db.execute(
            select(Provider).where(Provider.id == "pz"))).scalar_one()
        assert local.priority == 3, "non-credential edit should still apply"
        assert await _cb_state("pz") == "open", \
            "breaker must stay open when credentials are unchanged"
