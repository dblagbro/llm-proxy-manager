#!/bin/bash
# Entrypoint: launches the supervisord-managed sidecars (Xvfb, fluxbox,
# x11vnc, websockify) in the background, waits for the X display to be
# ready, then exec's the FastAPI app as PID-equivalent so the container
# stops cleanly on docker-stop.
set -e

# Boot the supervisord stack in the background.
/usr/bin/supervisord -c /etc/supervisor/supervisord.conf -n &
SUPERVISORD_PID=$!

# Wait for Xvfb to come up — ~3s on a warm container.
for i in $(seq 1 30); do
    if [ -S /tmp/.X11-unix/X99 ]; then
        echo "[start.sh] Xvfb display :99 is up"
        break
    fi
    sleep 0.2
done

if [ ! -S /tmp/.X11-unix/X99 ]; then
    echo "[start.sh] FATAL: Xvfb did not come up within 6s" >&2
    exit 1
fi

# Trap SIGTERM/SIGINT and tear down supervisord with us so docker-stop is fast.
trap 'echo "[start.sh] received stop signal"; kill -TERM $SUPERVISORD_PID 2>/dev/null; wait $SUPERVISORD_PID 2>/dev/null; exit 0' TERM INT

# Run the FastAPI app in the foreground. uvicorn handles its own signal
# forwarding to the asyncio event loop.
exec uvicorn app:app \
    --host "${BRIDGE_HOST:-0.0.0.0}" \
    --port "${BRIDGE_PORT:-8443}" \
    --no-access-log
