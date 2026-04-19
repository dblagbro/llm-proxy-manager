"""
llm-proxy v2 — FastAPI application entry point.
"""
import logging
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import os

from app.config import settings
from app.models.database import init_db, AsyncSessionLocal
from app.auth.admin import ensure_default_admin
from app.monitoring.status import start_monitor, register_provider
from app.monitoring.notifications import alert_cluster_node_down, alert_all_providers_down
from app.cluster.manager import start_cluster, get_cluster_status
from app.models.db import Provider
from sqlalchemy import select

# ── Routers ──────────────────────────────────────────────────────────────────
from app.api.messages import router as messages_router
from app.api.completions import router as completions_router
from app.api.auth import router as auth_router
from app.api.providers import router as providers_router
from app.api.apikeys import router as apikeys_router
from app.api.users import router as users_router
from app.api.cluster import router as cluster_router
from app.api.monitoring import router as monitoring_router

logging.basicConfig(level=settings.log_level.upper())
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB init + default admin
    await init_db()
    async with AsyncSessionLocal() as db:
        await ensure_default_admin(db)

        # Register all providers with status monitor
        result = await db.execute(select(Provider))
        for p in result.scalars().all():
            register_provider(p.id, p.provider_type)

    # Start background tasks
    start_monitor(notify_fn=_notify_provider_degraded)
    start_cluster(
        db_factory=AsyncSessionLocal,
        notify_fn=alert_cluster_node_down,
    )

    logger.info("llm-proxy v2 started", port=settings.port, cluster=settings.cluster_enabled)
    yield
    logger.info("llm-proxy v2 shutting down")


async def _notify_provider_degraded(severity: str, message: str, provider_id: str):
    from app.monitoring.notifications import send_alert
    await send_alert(severity, "Provider status degraded", message, provider_id=provider_id)


app = FastAPI(
    title="llm-proxy",
    version="2.0.0",
    description="Self-hosted LLM routing gateway — LMRH protocol + CoT-E augmentation",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["LLM-Capability", "X-Provider"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    ms = int((time.monotonic() - start) * 1000)
    # Skip logging for health checks and static files to reduce noise
    if request.url.path not in ("/health", "/favicon.ico"):
        logger.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=ms,
        )
    return response


# ── Core LLM endpoints (same paths as v1) ────────────────────────────────────
app.include_router(messages_router)
app.include_router(completions_router)

# ── Admin API ────────────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(providers_router)
app.include_router(apikeys_router)
app.include_router(users_router)
app.include_router(cluster_router)
app.include_router(monitoring_router)

# ── Static files (web dashboard) — mounted last ──────────────────────────────
_static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.isdir(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")


# ── Utility endpoints ────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/version")
async def version():
    return {"service": "llm-proxy", "version": "2.0.0", "docs": "/docs"}
