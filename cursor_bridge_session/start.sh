#!/bin/bash
# cursor-bridge-session entrypoint.
#
# Boots the supervisord stack (Xvfb / fluxbox / x11vnc / websockify),
# waits for Xvfb to be ACCEPTING X11 CONNECTIONS (not just have a
# socket file), then exec's the FastAPI app in the foreground.
#
# Same wait pattern as grok_bridge's start.sh — pre-existing
# learnings about Xvfb's connection-listener race are reused
# verbatim here. See BUG-025 in the v4.4-M-1 grok_bridge changelog
# for the post-mortem if the wait window needs tuning.
set -e

echo "[cursor-bridge-session] booting supervisord (Xvfb/fluxbox/x11vnc/websockify)"
/usr/bin/supervisord -c /etc/supervisor/supervisord.conf -n &
SUPERVISORD_PID=$!

echo "[cursor-bridge-session] waiting for Xvfb :99 to accept X11 connections"
for i in $(seq 1 60); do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        echo "[cursor-bridge-session] Xvfb :99 responsive after $((i * 5))ds"
        break
    fi
    sleep 0.5
done

if ! xdpyinfo -display :99 >/dev/null 2>&1; then
    echo "[cursor-bridge-session] FATAL: Xvfb did not accept connections within 30s" >&2
    ps auxf
    exit 1
fi

echo "[cursor-bridge-session] starting FastAPI on $BRIDGE_HOST:$BRIDGE_PORT"
exec python3 -m uvicorn app:app \
    --host "$BRIDGE_HOST" --port "$BRIDGE_PORT" \
    --log-level info
