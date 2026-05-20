#!/bin/bash
# Entrypoint: launches the supervisord-managed sidecars (Xvfb, fluxbox,
# x11vnc, websockify) in the background, waits for the X display to be
# READY (not just for its socket file to exist), then exec's the
# FastAPI app as PID-equivalent so the container stops cleanly on
# docker-stop.
#
# v4.4-M-1 hardening (BUG-025 fix):
#   - Use `xdpyinfo -display :99` to confirm the X server is actually
#     accepting connections, instead of just polling for the socket
#     file. Pre-M-1 the socket-file check raced with Xvfb's connection
#     listener: the file appeared before Xvfb finished its init, and
#     Chromium's connection attempt (during the FastAPI lifespan's
#     `launch_persistent_context`) failed with
#     `Missing X server or $DISPLAY`.
#   - Bumped the wait window from 6s → 30s with finer-grained polling.
#   - Explicitly log the readiness path so post-mortems are easier.
set -e

echo "[start.sh] booting supervisord stack (Xvfb / fluxbox / x11vnc / websockify)"
/usr/bin/supervisord -c /etc/supervisor/supervisord.conf -n &
SUPERVISORD_PID=$!

# Wait for Xvfb to be RESPONSIVE — try an actual X11 query against
# :99. Returns 0 when the X server replies; non-zero while it's still
# initialising or has crashed. Caps at 30s (60 × 0.5s) which is well
# beyond cold-start time (~3-5s on a healthy container).
echo "[start.sh] waiting for Xvfb :99 to accept X11 connections…"
for i in $(seq 1 60); do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        echo "[start.sh] Xvfb display :99 responsive after $((i * 5))ds"
        break
    fi
    sleep 0.5
done

if ! xdpyinfo -display :99 >/dev/null 2>&1; then
    echo "[start.sh] FATAL: Xvfb did not become responsive within 30s" >&2
    # Capture diagnostics so the container's stdout shows WHY this
    # failed — pre-M-1 the failure was silent except for Chromium's
    # later "Missing X server" message.
    echo "[start.sh] supervisord status:" >&2
    /usr/bin/supervisorctl -c /etc/supervisor/supervisord.conf status >&2 || true
    echo "[start.sh] /tmp/.X11-unix:" >&2
    ls -la /tmp/.X11-unix/ >&2 || true
    echo "[start.sh] xvfb log tail:" >&2
    tail -50 /var/log/supervisor/xvfb.err 2>&1 >&2 || true
    exit 1
fi

# Trap SIGTERM/SIGINT and tear down supervisord with us so docker-stop is fast.
trap 'echo "[start.sh] received stop signal"; kill -TERM $SUPERVISORD_PID 2>/dev/null; wait $SUPERVISORD_PID 2>/dev/null; exit 0' TERM INT

# Run the FastAPI app in the foreground. uvicorn handles its own signal
# forwarding to the asyncio event loop.
echo "[start.sh] launching uvicorn on ${BRIDGE_HOST:-0.0.0.0}:${BRIDGE_PORT:-8443}"
exec uvicorn app:app \
    --host "${BRIDGE_HOST:-0.0.0.0}" \
    --port "${BRIDGE_PORT:-8443}" \
    --no-access-log
