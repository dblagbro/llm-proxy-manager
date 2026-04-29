"""
llm-proxy v2 — FastAPI application entry point.
"""
import logging
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
import os

from app.__version__ import __version__
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
from app.api.models import router as models_router
from app.api.aliases import router as aliases_router
from app.api.auth import router as auth_router
from app.api.providers import router as providers_router
from app.api.apikeys import router as apikeys_router
from app.api.users import router as users_router
from app.api.cluster import router as cluster_router
from app.api.monitoring import router as monitoring_router
from app.api.settings_api import router as settings_router
from app.api.audit import router as audit_router
from app.api.oauth_capture import router as oauth_capture_router
from app.api.runs import router as runs_router
from app.observability.otel import init_tracer
from app.observability.prometheus import metrics_response, set_service_info, observe_circuit_breaker_state

logging.basicConfig(level=settings.log_level.upper())
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB init + default admin + runtime settings
    await init_db()
    async with AsyncSessionLocal() as db:
        await ensure_default_admin(db)
        from app import config_runtime
        await config_runtime.load(db)

        # v2.8.2: one-shot resolve any pre-existing priority ties so we
        # start each boot with a strict total order. Idempotent.
        from app.api.providers import normalize_priority_ties
        bumped = await normalize_priority_ties(db)
        if bumped:
            await db.commit()
            logger.info(f"normalized {bumped} priority tie(s) on startup")

        # Register all providers with status monitor + per-provider CB config
        result = await db.execute(select(Provider))
        providers = result.scalars().all()
        for p in providers:
            register_provider(p.id, p.provider_type, p.hold_down_sec, p.failure_threshold)
            observe_circuit_breaker_state(p.id, "closed")  # seed Prometheus gauge

    # Observability — Prometheus service info + OTEL tracer (graceful no-op when unset)
    set_service_info(version=__version__, node_id=settings.cluster_node_id or "")
    init_tracer(service_name="llm-proxy", version=__version__)

    # R2: recover any in-flight runs this node owned before restart. Spawns
    # a worker per recovered run; emits run_recovered events so the hub
    # timeline can render the boundary cleanly.
    try:
        from app.runs.worker import recover_orphans
        recovered = await recover_orphans()
        if recovered:
            logger.info(f"runs.recovered count={recovered}")
    except Exception as e:
        # Recovery failure must not block startup — log loud, keep going.
        logger.warning(f"runs recovery sweep failed: {e}")

    # Start background tasks
    start_monitor(notify_fn=_notify_provider_degraded)
    start_cluster(
        db_factory=AsyncSessionLocal,
        notify_fn=alert_cluster_node_down,
    )

    logger.info("llm-proxy v2 started port=%s cluster=%s", settings.port, settings.cluster_enabled)
    yield
    logger.info("llm-proxy v2 shutting down")


async def _notify_provider_degraded(severity: str, message: str, provider_id: str):
    from app.monitoring.notifications import send_alert
    await send_alert(severity, "Provider status degraded", message, provider_id=provider_id)


app = FastAPI(
    title="llm-proxy",
    version=__version__,
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
    expose_headers=[
        "LLM-Capability", "X-Provider", "X-Resolved-Model", "X-Token-Budget-Remaining",
        "X-Cache-Status", "X-Cache-Similarity", "X-Hedged-Winner",
        "X-Budget-Warning", "X-Budget-Daily-Remaining", "X-Budget-Hourly-Remaining",
        "X-Critique-Provider", "X-Cot-Samples", "X-Cot-Task-Branch",
        "X-Fallback-Chain",
        "X-Cascade", "X-Cascade-Reason", "X-Cascade-Grader",
        "X-Task-Auto-Detected", "X-Shadow-Queued",
        "LLM-Hint-Set",
        "X-Tool-Calls-Emitted",
        "X-Structured-Output-Attempts", "X-Structured-Output-Status",
        "X-Vision-Routed", "X-Context-Strategy-Applied",
        "X-Resolved-Provider", "X-Emulation-Level", "X-Unsupported-Feature",
        "X-PII-Masked",
        "X-Sort-Mode", "X-Auto-Routed", "X-Fallback-From",
    ],
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
app.include_router(models_router)

# ── Admin API ────────────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(providers_router)
app.include_router(apikeys_router)
app.include_router(users_router)
app.include_router(cluster_router)
app.include_router(monitoring_router)
app.include_router(settings_router)
app.include_router(aliases_router)
app.include_router(audit_router)
app.include_router(oauth_capture_router)
app.include_router(runs_router)

# ── Utility endpoints ────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": __version__}


@app.get("/version")
async def version():
    return {"service": "llm-proxy", "version": __version__, "docs": "/docs"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return await metrics_response()


# ── Static files (web dashboard) ─────────────────────────────────────────────
# Mount /assets directly so JS/CSS are served correctly.
# All other unknown paths return index.html for React Router (SPA catch-all).
_static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
_assets_dir = os.path.join(_static_dir, "assets")

if os.path.isdir(_assets_dir):
    app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

if os.path.isdir(_static_dir):
    # Serve favicon and other root-level static files explicitly
    @app.get("/favicon.svg", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        p = os.path.join(_static_dir, "favicon.svg")
        return FileResponse(p) if os.path.isfile(p) else JSONResponse({"detail": "Not Found"}, 404)

    @app.get("/icons.svg", include_in_schema=False)
    async def icons_svg():
        p = os.path.join(_static_dir, "icons.svg")
        return FileResponse(p) if os.path.isfile(p) else JSONResponse({"detail": "Not Found"}, 404)

    # SPA catch-all: return index.html for all unmatched GET paths
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_catch_all(full_path: str):
        index = os.path.join(_static_dir, "index.html")
        if os.path.isfile(index):
            # v2.7.6 BUG-015: prevent browsers from caching the SPA shell so
            # they always pick up the latest fingerprinted asset bundle after
            # a deploy. Bundles themselves are content-hashed and immutable,
            # so they're safe to cache long.
            return FileResponse(
                index,
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )
        raise HTTPException(404, "Not found")
