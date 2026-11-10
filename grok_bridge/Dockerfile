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
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble@sha256:330d401ae23c905896a0c26c66c36fc971b621519e7b11b435a8328d7bf6e031

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1 \
    BRIDGE_STATE_DIR=/data/playwright-state \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=8443

# System packages — Xvfb, x11vnc, noVNC + websockify, plus a couple of
# small fonts so grok.com renders correctly (no font fallback boxes).
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        x11-utils \
        novnc \
        websockify \
        fluxbox \
        fonts-liberation \
        fonts-noto-color-emoji \
        ca-certificates \
        curl \
        supervisor \
    && rm -rf /var/lib/apt/lists/*
# v4.4-M-1 (BUG-025): x11-utils added for `xdpyinfo`, used in start.sh
# to verify the X server is RESPONSIVE (not just that its socket file
# exists). The socket-file-only readiness check in the pre-M-1 image
# raced with Chromium's connection attempt and produced
# `Missing X server or $DISPLAY` on every fresh container boot.

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/grok-venv
ENV PATH="/opt/grok-venv/bin:${PATH}"
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip==26.3 \
    && pip install --no-cache-dir -r /app/requirements.txt && pip check

COPY bridge_security.py /app/bridge_security.py
COPY app.py /app/app.py
COPY start.sh /app/start.sh
COPY supervisord.conf /etc/supervisor/conf.d/grok-bridge.conf
RUN chmod +x /app/start.sh

# Persistent state lives under /data — mount a volume here in compose.
RUN mkdir -p /data/playwright-state /var/log/supervisor

EXPOSE 8443

CMD ["/app/start.sh"]
