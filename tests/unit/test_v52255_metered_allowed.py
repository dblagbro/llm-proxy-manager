"""v5.22.55 — per-key ``metered_allowed`` guard.

A key with metered_allowed=False may only route to subscription
(unmetered) providers. When those are down the request degrades to the
"no providers" RuntimeError (503) instead of failing over to a metered
provider (tax-ai-analyzer incident: failover to a metered OpenAI account).
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

from app.api.apikeys import KeyUpdate, update_key  # noqa: E402
from app.models.db import ApiKey, Base, Provider  # noqa: E402
from app.routing.router import select_provider  # noqa: E402


async def _fresh_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _admin():
    return types.SimpleNamespace(user_id="u-1", username="admin", role="admin")


def _provider(pid, ptype, cost_class=None, priority=1):
    return Provider(
        id=pid, name=pid, provider_type=ptype, enabled=True,
        priority=priority, default_model="m", cost_class=cost_class,
    )


async def _seed(Session, *, metered_allowed, providers):
    async with Session() as db:
        kw = {} if metered_allowed is None else {"metered_allowed": metered_allowed}
        db.add(ApiKey(id="k-1", name="t", key_hash="h", key_prefix="p", **kw))
        for p in providers:
            db.add(p)
        await db.commit()


# ── Model / API ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_is_true():
    Session = await _fresh_db()
    await _seed(Session, metered_allowed=None, providers=[])
    async with Session() as db:
        k = (await db.execute(select(ApiKey))).scalar_one()
    assert k.metered_allowed is True


@pytest.mark.asyncio
async def test_patch_sets_and_reads_metered_allowed():
    Session = await _fresh_db()
    await _seed(Session, metered_allowed=None, providers=[])
    async with Session() as db:
        res = await update_key(
            key_id="k-1", body=KeyUpdate(metered_allowed=False), db=db, user=_admin()
        )
    assert res["metered_allowed"] is False
    async with Session() as db:
        k = (await db.execute(select(ApiKey))).scalar_one()
        assert k.metered_allowed is False
        # omitted field leaves it untouched
        res = await update_key(
            key_id="k-1", body=KeyUpdate(name="x"), db=db, user=_admin()
        )
    assert res["metered_allowed"] is False


# ── Router filter ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_false_excludes_metered_providers():
    Session = await _fresh_db()
    await _seed(Session, metered_allowed=False, providers=[
        _provider("metered", "openai", priority=1),                 # per_call
        _provider("sub", "claude-oauth", priority=5),               # derived subscription
    ])
    async with Session() as db:
        r = await select_provider(db, None, api_key_id="k-1")
    assert r.provider.id == "sub"


@pytest.mark.asyncio
async def test_explicit_cost_class_overrides_type():
    Session = await _fresh_db()
    await _seed(Session, metered_allowed=False, providers=[
        _provider("flat", "openai", cost_class="subscription", priority=9),
        _provider("oauth-metered", "claude-oauth", cost_class="per_call", priority=1),
    ])
    async with Session() as db:
        r = await select_provider(db, None, api_key_id="k-1")
    assert r.provider.id == "flat"


@pytest.mark.asyncio
async def test_false_with_only_metered_raises_no_provider():
    Session = await _fresh_db()
    await _seed(Session, metered_allowed=False, providers=[
        _provider("metered", "openai"),
    ])
    async with Session() as db:
        with pytest.raises(RuntimeError, match="No unmetered providers"):
            await select_provider(db, None, api_key_id="k-1")


@pytest.mark.asyncio
async def test_unmetered_down_does_not_fail_over_to_metered():
    """Subscription provider's breaker is open: must NOT fall to metered."""
    Session = await _fresh_db()
    await _seed(Session, metered_allowed=False, providers=[
        _provider("metered", "openai", priority=1),
        _provider("sub", "claude-oauth", priority=5),
    ])

    async def _avail(pid):
        return pid != "sub"

    async with Session() as db:
        with patch("app.routing.router.is_available", _avail):
            with pytest.raises(RuntimeError, match="circuit breakers open"):
                await select_provider(db, None, api_key_id="k-1")
        # Control: a default key in the same state DOES fail over to metered.
        k = (await db.execute(select(ApiKey))).scalar_one()
        k.metered_allowed = True
        await db.commit()
        with patch("app.routing.router.is_available", _avail):
            r = await select_provider(db, None, api_key_id="k-1")
    assert r.provider.id == "metered"


@pytest.mark.asyncio
async def test_default_true_leaves_metered_routable():
    Session = await _fresh_db()
    await _seed(Session, metered_allowed=True, providers=[
        _provider("metered", "openai", priority=1),
        _provider("sub", "claude-oauth", priority=5),
    ])
    async with Session() as db:
        r = await select_provider(db, None, api_key_id="k-1")
    assert r.provider.id == "metered"


# ── Cluster sync ────────────────────────────────────────────────────


def test_push_payload_includes_metered_allowed():
    from pathlib import Path
    src = Path("app/cluster/manager.py").read_text()
    assert '"metered_allowed"' in src[src.index("keys = ["):]


@pytest.mark.asyncio
async def test_sync_round_trips_metered_allowed():
    from app.cluster.sync import apply_sync
    Session = await _fresh_db()
    await _seed(Session, metered_allowed=None, providers=[])
    base = {"id": "k-1", "name": "t", "key_hash": "h", "key_prefix": "p"}
    async with Session() as db:
        await apply_sync(db, {"api_keys": [{**base, "metered_allowed": False}]})
        await db.commit()
        k = (await db.execute(select(ApiKey))).scalar_one()
        assert k.metered_allowed is False
        # legacy peer omitting the key must not clobber it
        await apply_sync(db, {"api_keys": [dict(base)]})
        await db.commit()
        await db.refresh(k)
        assert k.metered_allowed is False
        # insert path
        await apply_sync(db, {"api_keys": [
            {"id": "k-2", "name": "n", "key_hash": "h2", "key_prefix": "p2",
             "metered_allowed": False}]})
        await db.commit()
        k2 = (await db.execute(select(ApiKey).where(ApiKey.id == "k-2"))).scalar_one()
        assert k2.metered_allowed is False
