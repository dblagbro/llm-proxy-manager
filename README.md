# llm-proxy v2

Self-hosted LLM routing gateway — Python/FastAPI rewrite of llm-proxy v1.

**LMRH semantic routing · circuit breaker failover · CoT-E augmentation · cluster sync · React dashboard**

## Access

| Node | URL |
|------|-----|
| tmrwww01 | https://www.voipguru.org/llm-proxy2/ |
| tmrwww02 | https://www.voipguru.org/llm-proxy2/ |
| c1conversations-avaya-01-s23 | https://\<c1-domain\>/llm-proxy2/ |

**Default login**: `admin` / `admin` — change immediately after first boot.

## Stack

- **Backend**: Python 3.13, FastAPI, SQLite (aiosqlite), uvicorn
- **Frontend**: React 19, TypeScript, Vite, TailwindCSS v4, TanStack Query v5
- **Auth**: bcrypt passwords, HTTP-only session cookies, API key auth (`x-api-key`)
- **Deployment**: Docker, served at `/llm-proxy2/` via nginx reverse proxy

## Quick Start (Docker)

The service is already added to the main `docker-compose.yml` on all 3 nodes.
Build and start from `/home/dblagbro/docker/`:

```bash
sudo docker compose build llm-proxy2
sudo docker compose up -d --no-deps llm-proxy2
```

## nginx

Location config: `/home/dblagbro/docker/config/nginx/projects-locations.d/llm-proxy2.conf`

Three locations:
- `~ ^/llm-proxy2/(api/monitoring/activity/stream)` — SSE, unbuffered, 86400s timeout
- `~ ^/llm-proxy2/(v1/messages|v1/chat/completions)` — streaming LLM API, unbuffered
- `/llm-proxy2/` — all other traffic (UI, admin API, health)

After any nginx config change: `sudo docker exec nginx nginx -s reload`

## API

### Authentication

```bash
# Admin session login (sets session cookie)
curl -c cookies.txt -X POST https://www.voipguru.org/llm-proxy2/api/auth/login \
  -d "username=admin&password=admin"

# LLM API call with API key
curl -H "x-api-key: llmp2-your-key" \
  https://www.voipguru.org/llm-proxy2/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4-6", "max_tokens": 1024, "messages": [{"role":"user","content":"Hello"}]}'
```

### Health

```bash
curl https://www.voipguru.org/llm-proxy2/health
# {"status":"healthy","version":"2.7.5","nodeId":"...","totalProviders":N,"healthyProviders":N,...}
```

### LLM Endpoints (same paths as v1)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/messages` | Anthropic-format completions |
| POST | `/v1/chat/completions` | OpenAI-format completions |

### Admin API

| Method | Path | Description |
|--------|------|-------------|
| GET/POST | `/api/providers` | List / add providers |
| GET/POST | `/api/keys` | List / create API keys |
| GET/POST | `/api/users` | List / create users |
| GET | `/api/monitoring/status` | Provider health + circuit breaker states |
| GET | `/api/monitoring/activity` | Activity log (paginated) |
| GET | `/api/monitoring/activity/stream` | SSE live activity stream |
| GET | `/cluster/status` | Cluster node status |
| POST | `/cluster/circuit-breaker/{id}/reset` | Force-close a circuit breaker |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | `change-me-in-production` | Session signing key |
| `DATABASE_URL` | `sqlite+aiosqlite:////app/data/llmproxy.db` | DB path |
| `LOG_LEVEL` | `info` | Logging level |
| `CIRCUIT_BREAKER_THRESHOLD` | `3` | Failures before opening circuit |
| `CIRCUIT_BREAKER_TIMEOUT_SEC` | `60` | Seconds circuit stays open |
| `HOLD_DOWN_SEC` | `120` | Post-failure provider hold-down |
| `CLUSTER_ENABLED` | `false` | Enable cluster mode |
| `CLUSTER_NODE_ID` | — | Unique node identifier |
| `CLUSTER_NODE_URL` | — | This node's public URL |
| `CLUSTER_PEERS` | — | `id:url,id:url` comma-separated peers |
| `CLUSTER_SYNC_SECRET` | — | HMAC shared secret for cluster sync |
| `SMTP_ENABLED` | `false` | Enable email alerts |

## LMRH Protocol

Route requests by task type, cost, latency, region, or modality:

```bash
curl -X POST .../v1/messages \
  -H "LLM-Hint: task=reasoning, cost=economy, region=us" \
  -H "x-api-key: your-key" \
  -d '{"model": "claude-sonnet-4-6", ...}'
# Response includes: LLM-Capability: v=1, provider=google, model=gemini-2.5-flash, ...
```

Append `;require` to any affinity to make it a hard constraint (returns 503 if unmet).

## Cluster Mode

All 3 nodes sync users and API keys via HMAC-authenticated `/cluster/sync` POST calls.
Each node maintains independent provider configurations and priorities.

```bash
curl -b cookies.txt https://www.voipguru.org/llm-proxy2/cluster/status
```

## Development

```bash
cd /home/dblagbro/llm-proxy-v2

# Backend (hot reload)
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 3000

# Frontend (dev server)
cd frontend && npm install && npm run dev

# Integration tests (Playwright)
python -m pytest tests/integration/test_playwright_ui.py -v
```

## Rebuild All 3 Nodes

```bash
# On each node — build new image, recreate only llm-proxy2
cd /home/dblagbro/docker
sudo docker compose build llm-proxy2
sudo docker compose up -d --force-recreate --no-deps llm-proxy2

# Verify
curl -s https://www.voipguru.org/llm-proxy2/health | jq
```

## Provider Types

| Type | Auth | Notes |
|------|------|-------|
| `anthropic` | `x-api-key` (sk-ant-api03-…) | Standard Anthropic API keys |
| `openai` | `Authorization: Bearer` | OpenAI platform + any OpenAI-compatible endpoint |
| `google` | API key | Gemini / Vertex |
| `vertex` | Service account | Google Cloud Vertex AI |
| `grok` | `Authorization: Bearer` | xAI |
| `ollama` | none (local) | Self-hosted Ollama |
| `compatible` | varies | Generic OpenAI-compatible (LM Studio, LocalAI, etc.) |
| `claude-oauth` | OAuth `Bearer sk-ant-oat…` | **Claude Pro Max subscription** — see below |

### Claude Pro Max subscription (`claude-oauth`) — v2.7.1+

Add a Claude Pro Max account as a provider without needing an Anthropic API key.
In the Providers UI, pick `provider_type: claude-oauth` and click **Generate
Auth URL**. Open the URL in a browser where you're signed in to claude.ai,
approve, and paste the `CODE#STATE` string from the success page back into the
form. The proxy handles the PKCE token exchange and stores access + refresh
tokens (Fernet-encrypted at rest).

Traffic to `claude-oauth` providers bypasses litellm and hits
`platform.claude.com/v1/messages` directly with the exact Claude Code header
bundle (Bearer auth + beta flags + required system-prompt marker). Haiku models
automatically drop the `context-1m` beta flag that the Pro Max tier doesn't
grant. Scan Models, Test Provider, tool_use, streaming, vision, and prompt
caching all work end-to-end — see `scripts/test_claude_oauth_live.py`.

## Key Differences from v1

| Feature | v1 (Node.js) | v2 (Python) |
|---------|-------------|-------------|
| Runtime | Node.js + Express | Python 3.13 + FastAPI |
| DB | JSON files | SQLite (async) |
| Frontend | Server-rendered EJS | React 19 SPA |
| Auth | express-session | HTTP-only cookies + bcrypt |
| URL path | `/llmProxy/` | `/llm-proxy2/` |
| Port (internal) | 3000 | 3000 |
