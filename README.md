# LLM Proxy Manager

**Version**: 1.4.13
**Last Updated**: April 2026

Production-ready multi-provider LLM API proxy with automatic failover, streaming support, cost tracking, intelligent routing, and comprehensive web-based management.

## Features

- **Anthropic API Compatible** — Works seamlessly with Claude Code CLI and any Anthropic SDK client
- **Multi-Provider Support** — Anthropic Claude, Google Gemini, Google Vertex AI, OpenAI, Grok (xAI), Ollama, OpenAI-compatible endpoints
- **Scan Models** — Edit Provider form queries the provider's live API and lists all available models; drag to reorder, check/uncheck to enable per-model
- **Streaming Support** — Server-Sent Events (SSE) streaming for all providers
- **Automatic Failover** — 3-pass routing with hold-down circuit breaker; tries providers in priority order
- **Capability Router** — Automatically skips providers that can't handle the request (tool calls, vision, context window)
- **Turn Validator** — Detects and warns on malformed Gemini turn sequences before sending
- **XML Sentinel** — Detects bad model output patterns (leaked internal XML/function tags) and fails over automatically
- **Context Window Auto-Truncation** — Trims oldest messages before sending if request exceeds 85% of provider's context window
- **Conductor/Worker Parallel Racing** — When `CONDUCTOR_MODE=true`, races top N providers simultaneously; first valid response wins
- **Cost Tracking** — Real-time token usage and cost calculation per provider with fuzzy model name matching
- **Analytics Dashboard** — Total requests, cost, tokens, success rate; per-provider sparklines; time windows (Last Hour / 24h / 7d / All Time)
- **Per-Provider Chat Logs** — Human-readable conversation logs per provider with IP, API key name, and correlation ID; viewable in the Web UI with live SSE streaming
- **Per-API-Key Rate Limiting** — Optional requests/minute and requests/day quotas per client key (disabled by default)
- **Session Management** — Configurable login timeout (default 8 hours), stable SESSION_SECRET across restarts, active session registry with revocation
- **Password Reset** — Email-based forgot password with secure token generation (1-hour expiry, one-time use)
- **User Management** — Multi-user support with profile management and email notifications
- **SMTP Integration** — Configurable email for alerts and password resets
- **Web UI** — Real-time monitoring, cost tracking, circuit breaker status, configuration, and chat log viewer
- **Cluster Support** — Multi-node deployment with heartbeat sync, config sync, and tombstone-based provider deletion propagation
- **Docker Ready** — Single-container or cluster deployment via docker-compose

## Security

- API keys are always masked in `/api/config` — full key only revealed via authenticated `/api/provider-apikey/:id` endpoint when user clicks Show
- Port 3000 is never exposed to LAN — container communicates with nginx via internal Docker network only
- Default admin password: `Super*120120` (configurable via `DEFAULT_ADMIN_PASSWORD` env var)
- Client API key validation covers all paths (fixed bypass bug in v1.4.9)
- Cluster HMAC signature verification (fixed RangeError crash in v1.4.6)

## Deployment

### Docker Compose (Recommended)

Add to your existing `docker-compose.yml`:

```yaml
  llm-proxy-manager:
    image: dblagbro/llm-proxy-manager:latest
    container_name: llm-proxy-manager
    restart: unless-stopped
    environment:
      - NODE_ENV=production
      - CLUSTER_ENABLED=true
      - CLUSTER_NODE_ID=www1
      - CLUSTER_NODE_NAME=LLM Proxy www1
    volumes:
      - /opt/llm-proxy-data/config:/app/config
      - /opt/llm-proxy-data/logs:/app/logs
    networks:
      - default
```

Config is persisted at `/opt/llm-proxy-data/config/providers.json` on the host.

### Using with nginx

nginx must be on the same Docker network. Proxy by container hostname:

```nginx
location /llmProxy/ {
    proxy_pass http://llm-proxy-manager:3000/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 300s;
    proxy_send_timeout 300s;
    proxy_read_timeout 300s;
}
```

### Manual / Development

```bash
npm install
npm start        # production
npm run dev      # nodemon watch mode
```

## Using with Claude Code CLI

```bash
export ANTHROPIC_API_KEY="your-llm-proxy-client-key"
export ANTHROPIC_BASE_URL="https://www.voipguru.org/llmProxy"
cc "hello world"
```

## Web Management UI

The Web UI provides:

- **Provider panels** — enable/disable, drag to reorder priority, edit settings, run tests, scan live models
- **📋 Log button** — per-provider chat log viewer with live SSE stream toggle
- **Analytics Dashboard** — sparkline charts per provider, time-window selector, cost/token/latency tiles
- **Statistics** — per-provider request/success/failure counts, latency, token usage, costs
- **Circuit breaker status** — CLOSED / HALF_OPEN / OPEN with hold-down timer, manual release
- **Cluster view** — node status, last heartbeat, sync state
- **Session management** — view and revoke active sessions
- **Settings** — SMTP, session timeout, client API keys with optional rate limits

## Per-Provider Chat Logs

Every request is logged to `/app/logs/chat-<provider-name>.log`:

```
[2026-04-01 14:23:11 UTC] ── REQUEST → My-Anthropic (pass 1, model: claude-sonnet-4-6) ──
source-ip: 192.168.1.10  key: Claude Code CLI  request-id: req-abc123
[USER]
What is the capital of France?

[2026-04-01 14:23:12 UTC] ── RESPONSE ← My-Anthropic ──
[ASSISTANT]
The capital of France is Paris.
latency=312ms  model=claude-sonnet-4-6  tokens=in:14/out:8  cost=$0.000163
```

## Routing Logic

### 3-Pass Failover

1. Request arrives in Anthropic format
2. Providers sorted by priority; held-down providers excluded
3. **Capability Router** filters providers that can't satisfy the request
4. For each remaining provider (pass 1–3):
   - Context window check → auto-truncate if needed
   - **Turn Validator** (Gemini) — checks for malformed turn sequences
   - Provider call with latency guard (`maxLatencyMs`, default 1800ms)
   - **XML Sentinel** — scans response for leaked internal tags; fails over if detected
5. Returns `503 overloaded_error` if all providers exhausted

### Hold-Down Circuit Breaker

Providers that fail are held down for a cooldown period. At 90% of the timer they get a retest. Hold-down state visible in Web UI; manually releasable.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/v1/messages` | API key | Main proxy endpoint (Anthropic format) |
| GET | `/health` | None | Health check + version |
| GET | `/api/config` | Session | Get config (API keys masked) |
| POST | `/api/config` | Session | Update config |
| GET | `/api/provider-apikey/:id` | Session | Get full (unmasked) API key for provider |
| GET | `/api/stats` | API key | Per-provider statistics |
| POST | `/api/stats/reset` | Session | Reset statistics |
| GET | `/api/provider-chat-log` | Session | Get chat log lines for a provider |
| GET | `/api/chat-log-stream` | Session | SSE stream of live chat log |
| GET | `/api/holddown-status` | Session | Circuit breaker state |
| POST | `/monitoring/holddown/release` | Session | Release provider from hold-down |
| POST | `/api/test-provider` | Session | Test a provider configuration |
| POST | `/api/scan-provider-models` | Session | Query provider API for available models |
| PATCH | `/api/client-keys/:id` | Session | Update client API key (rate limits etc.) |
| GET | `/api/sessions` | Session | List active sessions |
| DELETE | `/api/sessions/:id` | Session | Revoke a session |
| GET | `/cluster/status` | Session | Cluster node status |
| POST | `/cluster/heartbeat` | HMAC | Node heartbeat |
| GET | `/cluster/config` | HMAC | Pull config from peer |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3000` | HTTP port |
| `NODE_ENV` | `development` | Environment |
| `SESSION_SECRET` | *(random)* | Stable session secret — set this to keep sessions across restarts |
| `DEFAULT_ADMIN_PASSWORD` | `Super*120120` | Password for auto-created admin account on first run |
| `CLUSTER_ENABLED` | `false` | Enable cluster sync |
| `CLUSTER_NODE_ID` | *(hostname)* | This node's unique ID |
| `CLUSTER_NODE_NAME` | — | Human-readable node name |
| `CLUSTER_SYNC_SECRET` | — | HMAC secret for cluster peer authentication |
| `CONDUCTOR_MODE` | `false` | Enable parallel provider racing |
| `CONDUCTOR_WORKERS` | `2` | Number of providers to race simultaneously |
| `CONFIG_PATH` | `/app/config/providers.json` | Config file path |

## Logs

All logs in `/app/logs/` — each rotates at 500MB, retaining last 5 files:

| File | Contents |
|------|----------|
| `combined.log` | All requests and events (JSON) |
| `error.log` | Errors only (JSON) |
| `provider-<name>.log` | Per-provider structured log (JSON) |
| `chat-<name>.log` | Per-provider human-readable chat log |

## Backup

Runtime config is at `/opt/llm-proxy-data/config/providers.json`. Back this up — it contains all providers, API keys, client keys, and users.

Automated nightly backup script: `/home/dblagbro/docker/scripts/backup.sh`
Backup destination: `/mnt/s/router_and_LAN/backups/`
Retention: 14 daily → biweekly (180 days) → monthly (4 years) → permanent (one per year, forever)

## Troubleshooting

**502 Bad Gateway**
Ensure nginx and `llm-proxy-manager` are on the same Docker network. nginx config must use the container hostname (`llm-proxy-manager`), not `localhost` or an IP.

**All providers failing**
- Check API keys in provider config
- Check hold-down status in Web UI
- View logs: `docker exec llm-proxy-manager tail -50 /app/logs/error.log`

**Version shows wrong number in UI**
Version is read dynamically from `/health` on page load — if stale, hard-refresh the browser.

**Session logs out quickly**
Set `sessionTimeoutMinutes` in Settings (default 480 = 8 hours). Set `SESSION_SECRET` env var so sessions survive container restarts.

## Architecture

```
Client (Claude Code CLI / any Anthropic SDK)
           │  Anthropic API format  +  client API key
           ▼
   ┌───────────────────┐
   │  LLM Proxy Manager│  (behind nginx, internal Docker network)
   └───────┬───────────┘
           │
   ┌───────▼────────────────────────────────────┐
   │  Routing Pipeline                           │
   │  1. API key validation                      │
   │  2. Hold-down filter                        │
   │  3. Capability router                       │
   │  4. [Optional] Conductor parallel race      │
   │  5. 3-pass loop:                            │
   │     a. Context window check + truncation    │
   │     b. Turn validator (Gemini)              │
   │     c. Provider call + latency guard        │
   │     d. XML sentinel check                   │
   └───────┬────────────────────────────────────┘
           │
   ┌───────┴──────┬──────────┬────────┬──────────┐
   ▼              ▼          ▼        ▼          ▼
Anthropic     Google      OpenAI    Grok      Ollama /
 Claude        Gemini /    GPT      xAI     Compatible
               Vertex
```

## License

MIT
