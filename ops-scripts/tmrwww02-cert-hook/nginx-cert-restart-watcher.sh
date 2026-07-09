#!/bin/bash
# v5.12.1 — host-side watcher for the certbot deploy-hook marker.
#
# Polls /home/dblagbro/docker/config/certbot/conf/.nginx-restart-needed
# every 60s. When present, runs `docker restart nginx` and deletes the
# marker. Logs each action to journald via stdout/stderr.
#
# Runs as a systemd service (nginx-cert-restart-watcher.service).
# Installed on tmrwww02 only — tmrwww01 + c1conv use different cert
# paths that don't need this watcher.
#
# Why this watcher exists: tmrwww02's nginx container picks up renewed
# certs via a host-mounted /etc/letsencrypt. The cert files are
# symlinks (live/<domain>/fullchain.pem -> ../../archive/<domain>/fullchainN.pem)
# and renewals swap the target inode. `nginx -s reload` re-reads the
# config paths but the kernel reuses the old inode behind the cached
# OpenSSL context, so a stale cert keeps serving until the nginx
# process exits. `docker restart nginx` forces a clean process restart,
# which is the minimum-viable fix until a hot-reload-aware nginx is in
# the picture.
#
# Operator-installed 2026-06-30.
set -eu

MARKER=/home/dblagbro/docker/config/certbot/conf/.nginx-restart-needed
POLL_INTERVAL_SEC=60

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

log "watcher started; polling $MARKER every ${POLL_INTERVAL_SEC}s"

while true; do
    if [ -f "$MARKER" ]; then
        log "marker present — restarting nginx"
        if sudo docker restart nginx; then
            log "nginx restart OK"
            rm -f "$MARKER" || log "WARN: could not remove marker"
        else
            log "ERROR: docker restart nginx FAILED — leaving marker for next tick"
        fi
    fi
    sleep "$POLL_INTERVAL_SEC"
done
