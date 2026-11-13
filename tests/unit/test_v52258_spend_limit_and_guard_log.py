"""v5.22.58 — spend-limit billing classification + metered-guard log line."""
from __future__ import annotations

import logging
import sys
import types

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.models.db import ApiKey, Base, Provider  # noqa: E402
from app.routing.router import select_provider  # noqa: E402


@pytest.mark.asyncio
async def test_guard_rejection_logs_key_and_dropped_count(caplog):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as db:
        db.add(ApiKey(id="k-guard", name="t", key_hash="h", key_prefix="p",
                      metered_allowed=False))
        db.add(Provider(id="p-openai", name="p-openai", provider_type="openai",
                        enabled=True, priority=1, default_model="m"))
        await db.commit()
    with caplog.at_level(logging.WARNING, logger="app.routing.router"):
        async with Session() as db:
            with pytest.raises(RuntimeError, match="No unmetered providers"):
                await select_provider(db, None, api_key_id="k-guard")
    msgs = [r.getMessage() for r in caplog.records
            if "metered_guard_rejected" in r.getMessage()]
    assert len(msgs) == 1
    assert "api_key_id=k-guard" in msgs[0]
    assert "dropped=1" in msgs[0]
