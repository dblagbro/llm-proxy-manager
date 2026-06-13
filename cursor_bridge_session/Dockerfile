# cursor-bridge-session — Playwright + Chromium + noVNC sidecar for
# silent cursor-oauth JWT rotation (v5.5.x noVNC backlog).
#
# Distinct from the existing ``llm-proxy2-cursor-bridge`` container,
# which is the JiuZ-Chn cursor-to-openai stateless API adapter. This
# sidecar holds the WorkOS cookie state in a persistent Chromium
# profile and re-runs the /loginDeepControl PKCE flow every ~24h so
# the operator never has to click "Re-authorize" again under steady
# state.
#
# Architecture mirrors ``grok_bridge/Dockerfile`` (already deployed).
# Same base image + same Xvfb / x11vnc / websockify stack.
#
# Endpoints exposed:
#   - /healthz       inner FastAPI liveness; what compose probes
#   - /vnc/          noVNC HTML5 client (for operator first-time auth)
#   - /api/auth      admin-token-gated session status
#   - /api/rotate    admin-token-gated force-rotate-now endpoint
#   - /api/status    JSON snapshot (last_rotation_at, jwt_exp,
#                                   logged_in, sync_state)
#
# v5.5.0 (THIS SHIP) is scaffold only: the Python app responds to
# /healthz but the rotation cycle + noVNC route are stubs. v5.5.1
# wires the Playwright PKCE replay; v5.5.2 wires the HMAC callback
# to llm-proxy2; v5.5.3 ships the operator-facing UI panel.

FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1 \
    BRIDGE_STATE_DIR=/data/playwright-state \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=8444

# System packages — same set as grok_bridge so the operational
# burden of a second sidecar is zero (mind-share, support cookbook,
# log shape all match).
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        x11-utils \
        novnc \
        websockify \
        fluxbox \
        supervisor \
        curl \
        ca-certificates \
        fonts-dejavu-core \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Python deps for the FastAPI app + HMAC callback.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

WORKDIR /app
COPY supervisord.conf /etc/supervisor/supervisord.conf
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh
COPY app.py /app/app.py

# Persistent profile directory — holds the WorkOS session cookie
# across container restarts.
VOLUME ["/data"]

EXPOSE 8444

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -sf http://localhost:8444/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/start.sh"]
