# grok-bridge — Playwright + Chromium + noVNC sidecar for llm-proxy2 grok-web.
#
# Maintains a persistent logged-in grok.com session via Playwright, exposes:
#   - /login        → noVNC client (live Chromium UI for one-time sign-in)
#   - /api/auth     → admin endpoint to query session status
#   - /api/chat     → llm-proxy2 calls this with a chat body; bridge does HTTP
#                     replay against grok.com using its live cookies and
#                     auto-refreshes the Playwright page on 401/403.
#
# Base image already includes Playwright + Chromium + system deps. We add:
#   - xvfb              virtual framebuffer (Chromium needs an X display)
#   - x11vnc            VNC server that exposes the framebuffer
#   - websockify+noVNC  HTML5 VNC client served over HTTP/WS
#   - python deps       FastAPI / uvicorn / httpx (already present in base)
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1 \
    BRIDGE_STATE_DIR=/data/playwright-state \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=8443

# System packages — Xvfb, x11vnc, noVNC + websockify, plus a couple of
# small fonts so grok.com renders correctly (no font fallback boxes).
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        novnc \
        websockify \
        fluxbox \
        fonts-liberation \
        fonts-noto-color-emoji \
        ca-certificates \
        curl \
        supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps. The base already has playwright + asyncio; we just need
# FastAPI + uvicorn for the HTTP surface.
RUN pip install --no-cache-dir \
        playwright==1.45.0 \
        fastapi==0.115.0 \
        uvicorn[standard]==0.32.0 \
        httpx==0.27.2 \
        python-multipart==0.0.12

COPY app.py /app/app.py
COPY start.sh /app/start.sh
COPY supervisord.conf /etc/supervisor/conf.d/grok-bridge.conf
RUN chmod +x /app/start.sh

# Persistent state lives under /data — mount a volume here in compose.
RUN mkdir -p /data/playwright-state /var/log/supervisor

EXPOSE 8443

CMD ["/app/start.sh"]
