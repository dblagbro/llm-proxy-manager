# cursor-oauth noVNC sidecar — design doc (v5.5.x project)

**Status:** Phase 1 scaffold ships as v5.5.0 (2026-06-12). Operator approval received same day.
**Background memory:** `project_backlog_cursor_oauth_novnc.md`

## Motivation

The current cursor-oauth flow expires every ~60 days (empirically 2026-08-01 for the operator's account). On expiry the operator must manually click "Re-authorize" in the Provider edit modal, sign in to Cursor again, and confirm. This is fine for one account but doesn't scale to N accounts, and the operator chose 2026-06-12 to ship a silent-rotation sidecar rather than wait for the first manual re-auth pain.

Empirically `oauth_refresh_token` is NULL on the only existing provider — so the cheap refresh-token path is not feasible. noVNC + persistent Chromium is the right move.

## Architecture

Two containers per llm-proxy2 deployment, isolated by purpose:

| Container | Stateless? | What it does |
|---|---|---|
| `llm-proxy2-cursor-bridge` (existing) | yes | JiuZ-Chn cursor-to-openai HTTP adapter. Forwards proxy requests to `api2.cursor.sh`. |
| `llm-proxy2-cursor-bridge-session` (NEW) | no | Holds a persistent WorkOS-authenticated Chromium profile. Re-runs `/loginDeepControl` PKCE every ~24h, captures fresh JWT, HMAC-POSTs it to llm-proxy2. |

The same separation grok-bridge follows — stateless adapter + stateful session holder.

## Phased ship plan

| Version | Scope | Effort |
|---|---|---|
| **v5.5.0** ← ships 2026-06-12 | Scaffold: directory, Dockerfile, supervisord, FastAPI skeleton, `/healthz`. NO Chromium launch, NO noVNC route, NO compose entry. Pin tests on file presence + healthz route shape. Design doc (this file). | ~2h |
| **v5.5.1** | Chromium launch via Playwright lifespan (persistent context under `/data/playwright-state`). `/vnc/` reverse-proxy to localhost:6080 websockify. nginx route `/llm-proxy2/cursor-bridge-session/vnc/` added with admin-session gate. Operator can open the noVNC tab and sign in to Cursor once. Compose entry added to `/home/dblagbro/docker/docker-compose.yml`. | ~4h |
| **v5.5.2** | PKCE generator + `/loginDeepControl` drive + `/auth/poll` cycle (port of `app/providers/cursor_oauth_flow.py` into the sidecar). 24h rotation cron inside the sidecar. HMAC-signed POST to new llm-proxy2 endpoint `POST /api/admin/cursor-oauth-rotate-callback` that updates `providers.api_key` + `oauth_expires_at`. | ~4h |
| **v5.5.3** | Operator-facing "Session health" panel on `ProvidersPage` showing last_rotation_at, jwt_exp, logged_in, sync_state. New backend endpoint `GET /api/admin/cursor-oauth-sessions` aggregates per-provider state from the sidecar's `/api/status`. | ~3h |
| **v5.5.4** (stretch) | Multi-account: one sidecar per cursor-oauth provider. Bridge label = provider name. Compose template generator script for adding additional providers. | ~3h |

Total: ~13h of engineering, plus 1-2h operator first-time auth + verification.

## Service shape

```
llm-proxy2-cursor-bridge-session/
├── Dockerfile        # Playwright base + xvfb/novnc/supervisord
├── requirements.txt  # fastapi + uvicorn + httpx + playwright
├── supervisord.conf  # xvfb -> fluxbox -> x11vnc -> websockify
├── start.sh          # boot script + Xvfb readiness wait
└── app.py            # FastAPI app, /healthz, /api/status, /api/rotate, /vnc/
```

## Compose entry (for v5.5.1 when actually wired)

Operator should add this block to `/home/dblagbro/docker/docker-compose.yml` after v5.5.1 ships. Mirror the existing `llm-proxy2-cursor-bridge` and `llm-proxy2-grok-bridge` blocks.

```yaml
  llm-proxy2-cursor-bridge-session:
    build: /home/dblagbro/llm-proxy-v2/cursor_bridge_session
    image: llm-proxy2-cursor-bridge-session:latest
    container_name: llm-proxy2-cursor-bridge-session
    restart: unless-stopped
    environment:
    - BRIDGE_TOKEN=${BRIDGE_TOKEN:-REMOVED-CREDENTIAL-ROTATED-20260828}
    - BRIDGE_PUBLIC_PATH=/llm-proxy2/cursor-bridge-session
    - LLM_PROXY_HMAC_KEY=${CURSOR_BRIDGE_HMAC_KEY:-rotate-me-in-prod}
    - LLM_PROXY_CALLBACK_URL=http://llm-proxy2:3000/api/admin/cursor-oauth-rotate-callback
    shm_size: 1gb
    volumes:
    - llm-proxy2-cursor-bridge-session-data:/data
    networks:
    - default
    healthcheck:
      test:
      - CMD-SHELL
      - curl -sf http://localhost:8444/healthz || exit 1
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s
```

And the named volume at the bottom:

```yaml
volumes:
  llm-proxy2-cursor-bridge-session-data:
```

## nginx route (for v5.5.1)

Operator should add to `/home/dblagbro/docker/config/nginx/projects-locations.d/llm-proxy2.conf`:

```nginx
location /llm-proxy2/cursor-bridge-session/ {
    # Auth: operator's llm-proxy2 admin cookie must be present.
    # The proxy app validates the session via /api/auth/me before
    # we hit this location (auth_request).
    auth_request /llm-proxy2/api/auth/me;
    error_page 401 = @cursor_bridge_session_login;

    rewrite ^/llm-proxy2/cursor-bridge-session/(.*) /$1 break;
    proxy_pass http://llm-proxy2-cursor-bridge-session:8444;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}

location @cursor_bridge_session_login {
    return 302 /llm-proxy2/login;
}
```

## HMAC callback (for v5.5.2)

The sidecar POSTs new JWTs to llm-proxy2:

```
POST /api/admin/cursor-oauth-rotate-callback
X-Bridge-HMAC: <hex(hmac_sha256(body, LLM_PROXY_HMAC_KEY))>
Content-Type: application/json

{
  "provider_id": "<uuid>",
  "rotated_at": "2026-06-15T03:00:00Z",
  "new_api_key": "user_<id>::<JWT>",
  "new_expires_at": 1788460800,
  "rotation_source": "scheduled" | "operator-triggered"
}
```

Llm-proxy2 side verifies HMAC, updates `providers.api_key` and `providers.oauth_expires_at`, writes a `compliance_policy_changes` audit row (api_key rotation is a security-sensitive event), invalidates the auth cache for that key.

## Why a NEW container instead of extending llm-proxy2-cursor-bridge

The existing `cursor-bridge` is the stateless API adapter (~5 MB upstream image, no Playwright, no Chromium). Adding noVNC + Playwright + persistent profile to it would:

1. Bloat its image (~1.5 GB Playwright base).
2. Tightly couple two unrelated lifecycles (a stateless adapter that restarts cleanly vs a stateful session holder whose restart costs an operator re-auth).
3. Make smoke harder (smoke runs a copy of the adapter; it has no reason to hold its own Cursor session).

Same architectural reasoning grok-bridge followed.

## v5.5.0 deliverables (THIS ship)

- [x] Directory created: `cursor_bridge_session/`
- [x] Dockerfile (Playwright base, Xvfb/x11vnc/novnc/websockify stack)
- [x] supervisord.conf (4-program stack)
- [x] start.sh (Xvfb readiness wait + uvicorn exec)
- [x] app.py (FastAPI with `/healthz` live; `/api/status`, `/api/rotate`, `/vnc/*` stubs)
- [x] requirements.txt
- [x] This design doc
- [x] Pin tests for scaffold file presence + `/healthz` shape
- [ ] Compose entry NOT yet wired (waits for v5.5.1)
- [ ] nginx route NOT yet wired (waits for v5.5.1)

The image will not be built or run as part of v5.5.0 — the scaffold is committed so v5.5.1's work has somewhere to land. Operator can review the design + dockerfile + compose-block shape before any container exists.
