# tmrwww02 LE cert auto-renewal hook (v5.12.1)

Wires Let's Encrypt cert auto-renewal on tmrwww02 to an automatic nginx
container restart. Without this, the dockerized certbot auto-renews the
cert (~30 days before expiry) but nginx keeps serving the old cert from
its in-memory OpenSSL context until someone manually restarts it.

## Architecture

```
certbot container          host (tmrwww02)              docker
─────────────────         ──────────────────           ─────────
renew succeeds                                          nginx (stale cert)
  ↓
deploy-hook fires                                            ↑
  touches marker         marker file in shared mount        │
                         /home/dblagbro/docker/             │
                         config/certbot/conf/               │
                         .nginx-restart-needed              │
                              ↓                             │
                         nginx-cert-restart-watcher         │
                         systemd service                    │
                           polls every 60s                  │
                           sees marker → restart nginx ─────┘
                           deletes marker
```

The marker pattern handles:

- **Idempotency** — multiple touches collapse to one restart.
- **Resilience** — if the watcher misses a tick, the next tick catches
  it. Marker stays until acted on.
- **Decoupling** — certbot container doesn't need docker.sock (security
  win — operator explicitly rejected the doD path).

## Files

- `touch-nginx-restart-marker.sh` — certbot deploy-hook. Runs INSIDE
  the certbot container after every successful renewal. Touches the
  marker at `/etc/letsencrypt/.nginx-restart-needed` (which is the
  shared bind mount).
- `nginx-cert-restart-watcher.sh` — host-side watcher daemon. Polls
  the marker file once per minute; on detect, runs `docker restart
  nginx`, deletes marker.
- `nginx-cert-restart-watcher.service` — systemd unit. `Restart=always`
  so daemon survives crashes.

## Install (one-time, root)

```bash
# Already installed on tmrwww02 2026-06-30 — these are the steps.

# Deploy-hook (lives in certbot's letsencrypt conf dir, root-owned)
sudo cp touch-nginx-restart-marker.sh \
    /home/dblagbro/docker/config/certbot/conf/renewal-hooks/deploy/
sudo chmod +x /home/dblagbro/docker/config/certbot/conf/renewal-hooks/deploy/touch-nginx-restart-marker.sh
sudo chown root:root /home/dblagbro/docker/config/certbot/conf/renewal-hooks/deploy/touch-nginx-restart-marker.sh

# Watcher script (dblagbro-owned)
sudo mkdir -p /home/dblagbro/docker/host-tools
sudo chown dblagbro:dblagbro /home/dblagbro/docker/host-tools
cp nginx-cert-restart-watcher.sh /home/dblagbro/docker/host-tools/
chmod +x /home/dblagbro/docker/host-tools/nginx-cert-restart-watcher.sh

# systemd unit
sudo cp nginx-cert-restart-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nginx-cert-restart-watcher.service
```

## Verify

```bash
# Watcher running:
systemctl status nginx-cert-restart-watcher.service

# Log tail:
journalctl -u nginx-cert-restart-watcher.service -f

# Smoke test (drops marker, watches for restart within 60s):
sudo touch /home/dblagbro/docker/config/certbot/conf/.nginx-restart-needed
journalctl -u nginx-cert-restart-watcher.service -f
# Wait up to 60s; expect:
#   "marker present — restarting nginx"
#   "nginx restart OK"
```

## When this fires

Approximately 30 days before cert expiry (current cert expires 2026-09-22 per
last renewal 2026-06-24, so next auto-renewal ~2026-08-23). The dockerized
certbot's `while :; do certbot renew; sleep 12h; done` entrypoint checks
every 12h; on the first renewal that succeeds, the deploy-hook fires.

## Not applied to

- **tmrwww01** — uses host-installed certbot with a different cert
  path; nginx restart is operationally easier there.
- **c1conv (GCP)** — runs its own cert provisioning via the C1 instance
  topology; separate from this watcher.
