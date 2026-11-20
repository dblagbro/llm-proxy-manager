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

# Apply supported distribution fixes on clean builds as well as the overlay.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

FROM base AS deps
# pip 26.3 updates its vendored msgpack and urllib3, not just top-level packages.
RUN python -m pip install --no-cache-dir --upgrade pip==26.3
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── runtime ──────────────────────────────────────────────────────────────────
FROM deps AS runtime
COPY app/ ./app/
COPY alembic.ini .
COPY alembic/ ./alembic/
# v3.5.10 — operator admin scripts (cleanup_alias_collisions, etc.).
# Shipped inside the image so the operator can run them via
# ``docker exec llm-proxy2 python3 -m tools.<name>`` without needing
# a host Python env with the proxy stack installed.
COPY tools/__init__.py tools/cleanup_alias_collisions.py ./tools/
# The public LMRH endpoints serve only these reviewed specifications.
COPY docs/draft-blagbrough-lmrh-00.md docs/lmrh-2.0-bidirectional.md ./docs/
COPY --from=frontend-build /ui/dist ./frontend/dist

# Source copied from the NFS tree can arrive without world r/x (dirs created with
# mode 0774); the chmod below lets the non-root runtime user read all code.
RUN addgroup --gid 640 appgroup && \
    adduser --uid 640 --gid 640 --no-create-home --disabled-password --gecos "" appuser && \
    mkdir -p /app/logs /app/data && \
    chown -R appuser:appgroup /app/logs /app/data && \
    chmod -R a+rX /app/app /app/alembic /app/tools /app/docs /app/frontend/dist

USER appuser

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000", "--workers", "1"]
