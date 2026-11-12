"""Serve the baked gateway auth router on synthetic SQLite; no app workers."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api.auth import router
from app.auth.admin import hash_password
from app.models.database import AsyncSessionLocal, engine
from app.models.db import Base, User


@asynccontextmanager
async def lifespan(app):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        for role in ("admin", "user"):
            db.add(
                User(
                    username="fixture-" + role,
                    role=role,
                    password_hash=hash_password("synthetic-" + "browser-fixture"),
                )
            )
        await db.commit()
    yield
    await engine.dispose()


app = FastAPI(lifespan=lifespan)
app.include_router(router)
observed = False


@app.get("/")
async def page():
    # Login uses the production router via browser fetch. The fixture page only
    # replaces the unrelated gateway UI; cookie behavior remains production code.
    return HTMLResponse(
        "<!doctype html><title>Synthetic gateway auth fixture</title>"
        '<p id="fixture">Synthetic gateway auth fixture</p>'
    )


@app.get("/health")
async def health():
    return {"ok": True, "synthetic": True}


@app.get("/input-observed")
async def input_observed():
    global observed
    observed = True
    return HTMLResponse(
        '<!doctype html><title>VNC input verified</title><body style="background:#00ff00">'
        "<h1>SYNTHETIC VNC INPUT VERIFIED</h1></body>"
    )


@app.get("/observations")
async def observations():
    return {"vnc_input_navigation": observed}
