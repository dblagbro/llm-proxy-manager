# LLM Proxy Manager

**Version**: 1.3.7
**Last Updated**: April 2026

Production-ready multi-provider LLM API proxy with automatic failover, streaming support, cost tracking, intelligent routing, and comprehensive web-based management.

## Features

- **Anthropic API Compatible** — Works seamlessly with Claude Code CLI and any Anthropic SDK client
- **Multi-Provider Support** — Anthropic Claude, Google Gemini, Google Vertex AI, OpenAI, Grok (xAI), Ollama, OpenAI-compatible endpoints
- **Streaming Support** — Server-Sent Events (SSE) streaming for all providers
- **Automatic Failover** — 3-pass routing with hold-down circuit breaker; tries providers in priority order
- **Capability Router** — Automatically skips providers that can't handle the request (tool calls, vision, context window)
- **Turn Validator** — Detects and warns on malformed Gemini turn sequences before sending
- **XML Sentinel** — Detects bad model output patterns (leaked internal XML/function tags) and fails over automatically
- **Cost Tracking** — Real-time token usage and cost calculation per provider with fuzzy model name matching
- **Per-Provider Chat Logs** — Human-readable conversation logs per provider, viewable in the Web UI
- **Session Management** — Configurable login timeout (default 8 hours), stable SESSION_SECRET across restarts
- **Password Reset** — Email-based forgot password with secure token generation
- **User Management** — Multi-user support with profile management and email notifications
- **SMTP Integration** — Configurable email for alerts and password resets
- **Web UI** — Real-time monitoring, cost tracking, circuit breaker status, configuration, and chat log viewer
- **Cluster Support** — Multi-node deployment with heartbeat sync
- **Docker Ready** — Single-container or cluster deployment

## Quick Start

### Docker (Recommended)

```bash
docker run -d \
  --name llm-proxy-manager \
  --restart unless-stopped \
  -p 3000:3000 \
  -v /opt/llm-proxy-data/config:/app/config \
  -v /opt/llm-proxy-data/logs:/app/logs \
  -e NODE_ENV=production \
  -e PORT=3000 \
  -e USE_SQLITE=true \
  -e SESSION_SECRET=your-stable-secret-here \
  dblagbro/llm-proxy-manager:latest
```

Access the Web UI at `http://localhost:3000/` — default login: `admin` / `admin123`

### Docker Compose

```bash
docker-compose up -d
```

### Manual / Development

```bash
npm install
npm start        # production
npm run dev      # nodemon watch mode
```

## Using with Claude Code CLI

```bash
export ANTHROPIC_API_KEY="any-value"           # proxy ignores this
export ANTHROPIC_BASE_URL="https://www.voipguru.org/llmProxy"
cc "hello world"
```

Or for local use:
```bash
export ANTHROPIC_BASE_URL="http://localhost:3000"
```

## Web Management UI

The Web UI provides:

- **Provider panels** — enable/disable, drag to reorder priority, edit settings, run tests
- **📋 Log button** — per-provider chat log viewer showing every request and response in readable chat format
- **Statistics** — per-provider request/success/failure counts, latency, token usage, costs
- **Circuit breaker status** — CLOSED / HALF_OPEN / OPEN with hold-down timer
- **Cluster view** — node status, last heartbeat, sync state
- **Settings** — SMTP, session timeout, API keys

## Per-Provider Chat Logs

Every request routed through a provider is logged to `/app/logs/chat-<provider-name>.log` in human-readable format:

```
[2026-04-01 14:23:11 UTC] ── REQUEST → My-Anthropic (pass 1, model: claude-sonnet-4-6) ──
────────────────────────────────────────────────────────────
[USER]
What is the capital of France?
────────────────────────────────────────────────────────────

[2026-04-01 14:23:12 UTC] ── RESPONSE ← My-Anthropic ──
[ASSISTANT]
The capital of France is Paris.
────────────────────────────────────────────────────────────
latency=312ms  model=claude-sonnet-4-6  tokens=in:14/out:8  cost=$0.000163
────────────────────────────────────────────────────────────
```

Failovers are logged inline:
```
[2026-04-01 14:23:12 UTC] ✗ FAILOVER from My-Anthropic (pass 1): Request failed (HTTP 529)
```

Click **📋 Log** on any provider panel in the Web UI to view and auto-refresh the log.

## Routing Logic

### 3-Pass Failover

1. Request arrives in Anthropic format
2. Providers sorted by priority, held-down providers excluded
3. **Capability Router** filters providers that can't satisfy the request:
   - Tool/function calls → requires `toolCalling` capability
   - Image content → requires `vision` capability
   - Long context → requires sufficient `contextWindow`
4. For each remaining provider (pass 1–3):
   - **Turn Validator** (Gemini) — checks for consecutive same-role turns, empty parts
   - **XML Sentinel** — scans response for leaked internal tags; fails over if detected
   - **Latency guard** — fails over if provider exceeds `maxLatencyMs` (default 1800ms)
5. Returns 503 if all providers exhausted

### Hold-Down Circuit Breaker

Providers that fail are held down for a cooldown period. At 90% of the timer they get a retest. The hold-down state is visible in the Web UI and can be manually released.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/messages` | Main proxy endpoint (Anthropic format) |
| GET | `/health` | Health check |
| GET | `/api/config` | Get config (API keys masked) |
| POST | `/api/config` | Update config |
| GET | `/api/stats` | Per-provider statistics |
| POST | `/api/stats/reset` | Reset statistics |
| GET | `/api/provider-chat-log?name=X&lines=N` | Get last N lines of chat log for provider X |
| GET | `/api/holddown-status` | Circuit breaker / hold-down state |
| POST | `/monitoring/holddown/release` | Release a provider from hold-down |
| POST | `/api/test-provider` | Test a provider configuration |
| POST | `/api/auth/login` | Login |
| POST | `/api/auth/logout` | Logout |
| GET | `/api/auth/check` | Check session |
| POST | `/api/auth/forgot-password` | Request password reset email |
| GET | `/cluster/status` | Cluster node status |
| POST | `/cluster/heartbeat` | Node heartbeat |

## Configuration

Config is stored in `/app/config/providers.json` (persisted via Docker volume).

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3000` | HTTP port |
| `NODE_ENV` | `development` | Environment |
| `USE_SQLITE` | `false` | Use SQLite for stats/logs (recommended for production) |
| `SESSION_SECRET` | *(random)* | Stable session secret — set this to keep sessions across restarts |
| `CLUSTER_ENABLED` | `false` | Enable cluster sync |
| `CLUSTER_NODE_URL` | — | This node's public URL |
| `CONFIG_PATH` | `/app/config/providers.json` | Config file path |

## Logs

All logs in `/app/logs/`:

| File | Contents |
|------|----------|
| `combined.log` | All requests and events (JSON) |
| `error.log` | Errors only (JSON) |
| `provider-<name>.log` | Per-provider structured log (JSON) |
| `chat-<name>.log` | Per-provider human-readable chat log |

```bash
# Tail combined log
docker exec llm-proxy-manager tail -f /app/logs/combined.log

# View chat log for a specific provider
docker exec llm-proxy-manager tail -100 /app/logs/chat-Google-Gemini-API.log
```

## Nginx Configuration

```nginx
location /llmProxy/ {
    proxy_pass http://localhost:3000/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_set_header Host $host;
    proxy_cache_bypass $http_upgrade;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 300s;
    proxy_send_timeout 300s;
    proxy_read_timeout 300s;
}
```

## Password Reset

1. Click "Forgot Password?" on login page
2. Enter username
3. Receive email with reset link (valid 1 hour, one-time use)
4. Click link, set new password

Requires SMTP configured under Settings → Email Notifications.

## Troubleshooting

**502 Bad Gateway on Node 2 (behind nginx container)**
Ensure the container is on the correct Docker network with the right alias:
```bash
docker run ... --network llm-proxy_llm-proxy-network --network-alias llm-proxy ...
```

**All providers failing**
- Check API keys in provider config
- Check hold-down status in Web UI (Monitoring section)
- View logs: `docker exec llm-proxy-manager tail -50 /app/logs/error.log`

**Session logs out quickly**
Set `sessionTimeoutMinutes` in Settings (default 480 = 8 hours). Also ensure `SESSION_SECRET` env var is set so sessions survive container restarts.

**Cost tracking showing $0**
Ensure `USE_SQLITE=true` — the result scoping fix requires v1.3.5+.

**Chat log not appearing**
The log file is created on first request through the provider. Make at least one request, then click 📋 Log.

## Architecture

```
Client (Claude Code CLI / any Anthropic SDK)
           │  Anthropic API format
           ▼
   ┌───────────────┐
   │  LLM Proxy    │
   │   Manager     │
   └───────┬───────┘
           │
   ┌───────▼────────────────────────────────┐
   │  Routing Pipeline                       │
   │  1. Hold-down filter                    │
   │  2. Capability router                   │
   │  3. 3-pass loop:                        │
   │     a. Turn validator (Gemini)          │
   │     b. Provider call + latency guard   │
   │     c. XML sentinel check              │
   └───────┬────────────────────────────────┘
           │
   ┌───────┴──────┬──────────┬────────┬─────────┐
   ▼              ▼          ▼        ▼         ▼
Anthropic     Google      OpenAI    Grok     Ollama /
 Claude        Gemini /    GPT      xAI    Compatible
               Vertex
```

## License

MIT
