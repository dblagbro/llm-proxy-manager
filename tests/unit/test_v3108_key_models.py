"""v3.10.8 — GET /api/keys/{key_id}/models — per-key effective model list.

The admin API-Keys page "Copy models" action lists every model an API
key can route to. A provider with ``Provider.owned_by_key_id`` set is
private to that key (v3.0.45 tenant scoping); ``NULL`` = shared. The
key's effective model set is the union of the ModelCapability rows of
the shared providers plus the ones it owns — disabled / deleted
providers and providers owned by *other* keys are excluded.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete


@pytest_asyncio.fixture
async def km_db():
    """Seed two keys + a spread of providers/capabilities, yield the
    session factory, clean up after."""
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import ApiKey, Base, Provider, ModelCapability

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def _purge(db):
        await db.execute(delete(ModelCapability).where(
            ModelCapability.provider_id.like("km-%")))
        await db.execute(delete(Provider).where(Provider.id.like("km-%")))
        await db.execute(delete(ApiKey).where(ApiKey.id.like("km-%")))

    async with AsyncSessionLocal() as db:
        await _purge(db)
        await db.commit()

    async with AsyncSessionLocal() as db:
        db.add(ApiKey(id="km-key", name="key-under-test",
                      key_prefix="llmp-km1", key_hash="km-hash-1", enabled=True))
        db.add(ApiKey(id="km-other", name="other-key",
                      key_prefix="llmp-km2", key_hash="km-hash-2", enabled=True))
        # shared provider — every key sees it
        db.add(Provider(id="km-shared", name="shared", provider_type="openai",
                        priority=10, enabled=True, default_model="shared-default"))
        # owned by km-key — only km-key sees it
        db.add(Provider(id="km-mine", name="mine", provider_type="anthropic",
                        priority=20, enabled=True, owned_by_key_id="km-key"))
        # owned by the OTHER key — km-key must NOT see it
        db.add(Provider(id="km-foreign", name="foreign", provider_type="grok",
                        priority=30, enabled=True, owned_by_key_id="km-other"))
        # disabled — excluded for everyone
        db.add(Provider(id="km-off", name="off", provider_type="google",
                        priority=40, enabled=False))
        for pid, mid in [
            ("km-shared", "shared-model-a"),
            ("km-shared", "shared-model-b"),
            ("km-mine", "mine-model"),
            ("km-mine", "shared-model-a"),   # dup model id across providers
            ("km-foreign", "foreign-model"),
            ("km-off", "disabled-model"),
        ]:
            db.add(ModelCapability(provider_id=pid, model_id=mid))
        await db.commit()

    yield AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await _purge(db)
        await db.commit()


async def _call(session_factory, key_id: str) -> dict:
    from app.api.apikeys import list_key_models
    async with session_factory() as db:
        return await list_key_models(key_id=key_id, db=db, _=None)


# ── scoping ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_includes_shared_and_owned_models(km_db):
    out = await _call(km_db, "km-key")
    models = set(out["models"])
    # shared provider's caps + default_model, and the owned provider's cap
    assert "shared-model-a" in models
    assert "shared-model-b" in models
    assert "shared-default" in models   # provider default_model is included
    assert "mine-model" in models


@pytest.mark.asyncio
async def test_excludes_foreign_owned_provider(km_db):
    """A provider owned by a DIFFERENT key must not leak into this
    key's model list."""
    out = await _call(km_db, "km-key")
    assert "foreign-model" not in out["models"]


@pytest.mark.asyncio
async def test_excludes_disabled_provider(km_db):
    out = await _call(km_db, "km-key")
    assert "disabled-model" not in out["models"]


@pytest.mark.asyncio
async def test_models_deduped_and_sorted(km_db):
    """shared-model-a is offered by two providers — it appears once.
    The list is case-insensitively sorted for stable copy output."""
    out = await _call(km_db, "km-key")
    models = out["models"]
    assert models.count("shared-model-a") == 1
    assert models == sorted(models, key=str.lower)
    assert out["count"] == len(models)
    # exactly the four expected models
    assert set(models) == {
        "shared-model-a", "shared-model-b", "shared-default", "mine-model",
    }


@pytest.mark.asyncio
async def test_other_key_sees_its_own_owned_provider(km_db):
    """The foreign key sees the shared provider + its own owned one,
    but not km-key's owned provider."""
    out = await _call(km_db, "km-other")
    models = set(out["models"])
    assert "foreign-model" in models
    assert "shared-model-a" in models
    assert "mine-model" not in models


@pytest.mark.asyncio
async def test_unknown_key_404s(km_db):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        await _call(km_db, "km-does-not-exist")
    assert ei.value.status_code == 404


# ── wiring ─────────────────────────────────────────────────────────


def test_endpoint_and_client_and_ui_wired():
    from pathlib import Path
    backend = Path("app/api/apikeys.py").read_text()
    assert '@router.get("/{key_id}/models")' in backend
    assert "owned_by_key_id" in backend

    client = Path("frontend/src/api/index.ts").read_text()
    assert "/api/keys/${id}/models" in client

    page = Path("frontend/src/pages/APIKeysPage.tsx").read_text()
    assert "copyKeyModels" in page
    # both delimiter options present
    assert "Copy as CSV" in page and "Copy one per line" in page
