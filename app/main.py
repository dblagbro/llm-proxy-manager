"""
llm-proxy v2 — FastAPI application entry point.
"""
import logging
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.models.database import init_db
from app.api.messages import router as messages_router
from app.api.completions import router as completions_router

logging.basicConfig(level=settings.log_level.upper())
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("llm-proxy v2 started", port=settings.port)
    yield
    logger.info("llm-proxy v2 shutting down")


app = FastAPI(
    title="llm-proxy",
    version="2.0.0",
    description="Self-hosted LLM routing gateway — LMRH protocol + CoT-E augmentation",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        ms=ms,
    )
    return response


# ── Core LLM endpoints (same paths as v1) ─────────────────────────────────────
app.include_router(messages_router)
app.include_router(completions_router)


# ── Health / status ──────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/")
async def root():
    return {"service": "llm-proxy", "version": "2.0.0", "docs": "/docs"}
