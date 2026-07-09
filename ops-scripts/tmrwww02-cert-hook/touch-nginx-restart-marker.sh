#!/bin/sh
# v5.12.1 — certbot deploy-hook for tmrwww02 nginx cert refresh.
#
# Runs INSIDE the certbot container after every successful certificate
# renewal. Touches a marker file in the shared /etc/letsencrypt mount
# (which is bind-mounted from the host at
# /home/dblagbro/docker/config/certbot/conf/).
#
# A host-side daemon (nginx-cert-restart-watcher.sh) polls for this
# marker once per minute; when seen, it runs `docker restart nginx`
# (which forces nginx to re-read the symlink target — necessary because
# `nginx -s reload` re-reads paths but doesn't pick up the new cert
# behind a symlink swap).
#
# Marker semantics: the watcher deletes the marker after acting on it,
# so a touch-on-no-change is benign and a multi-touch (multi-domain
# renewal) collapses to a single restart.
#
# Operator-installed 2026-06-30. See /home/dblagbro/llm-proxy-v2/CHANGELOG.md
# v5.12.1 for context.
set -eu
MARKER=/etc/letsencrypt/.nginx-restart-needed
touch "$MARKER"
echo "[certbot deploy-hook] marker touched: $MARKER (renewed: ${RENEWED_DOMAINS:-unknown})"
