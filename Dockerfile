# ── frontend build ──────────────────────────────────────────────────────────
FROM node:22-slim AS frontend-build

WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── python deps ─────────────────────────────────────────────────────────────
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── runtime ──────────────────────────────────────────────────────────────────
FROM deps AS runtime
COPY app/ ./app/
COPY alembic.ini .
COPY alembic/ ./alembic/
COPY --from=frontend-build /ui/dist ./frontend/dist

RUN addgroup --gid 1001 appgroup && \
    adduser --uid 1001 --gid 1001 --no-create-home --disabled-password appuser && \
    mkdir -p /app/logs /app/data && \
    chown -R appuser:appgroup /app/logs /app/data

USER appuser

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000", "--workers", "1"]
