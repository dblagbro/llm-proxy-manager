# llm-proxy v2

Self-hosted LLM routing gateway — Python/FastAPI rewrite of llm-proxy v1.

**LMRH semantic routing · circuit breaker failover · CoT-E augmentation · cluster sync · Run runtime · per-provider keep-alive probes · React dashboard**

Current version: **v3.0.29** (see [CHANGELOG.md](CHANGELOG.md))

## Access

| Node | URL |
|------|-----|
| tmrwww01 | https://www.voipguru.org/llm-proxy2/ |
| tmrwww02 | https://www2.voipguru.org/llm-proxy2/ |
| c1conversations-avaya-01-s23 | https://www.c1cx.com/llm-proxy2/ |
| Joint-smoke target | https://www.voipguru.org/llm-proxy2-smoke/ (pinned version, hub-team test target) |

**Default login on first boot**: `admin` / `admin` — change immediately after first login via the Users page.

## Stack

- **Backend**: Python 3.13, FastAPI, SQLite (aiosqlite, WAL mode), uvicorn
- **Frontend**: React 19, TypeScript, Vite, TailwindCSS v4, TanStack Query v5
- **Auth**: bcrypt passwords, HTTP-only session cookies, API key auth (`x-api-key` or `Authorization: Bearer`)
- **Deployment**: Docker, served at `/llm-proxy2/` via nginx reverse proxy

## Quick Start (Docker)

The service is in the main `docker-compose.yml` on all 3 nodes:

```bash
sudo docker compose build llm-proxy2
sudo docker compose up -d --force-recreate --no-deps llm-proxy2
```

## API surface

### Existing endpoints (v1-shape, frozen indefinitely)

| Method | Path | Description |
|--------|------|-------------|
| POST   | `/v1/messages`              | Anthropic-format completions (streaming + non-streaming) |
| POST   | `/v1/chat/completions`      | OpenAI-format completions (streaming + non-streaming) |
| POST   | `/v1/embeddings`            | OpenAI-format embeddings (v3.0.23+) |
| GET    | `/v1/models`                | OpenAI-shape model list (with `kind` tag — chat/embedding/image/audio, v3.0.23+) |
| GET    | `/health`                   | Public health probe (no auth) |
| GET    | `/metrics`                  | Prometheus metrics |
| GET    | `/lmrh.md`                  | LMRH 1.1 spec (public, served from `docs/draft-blagbrough-lmrh-00.md`) |
| GET    | `/lmrh/registry`            | Public list of built-in + runtime-registered LMRH dims (v3.0.25+) |
| GET    | `/lmrh/registry/{name}`     | Public single-dim lookup |
| POST   | `/lmrh/register`            | Auth-required, register a new LMRH dim (collision-resolved -2/-3 suffix) |
| POST   | `/lmrh/propose`             | Auth-required, queue a free-form proposal for operator review |
| DELETE | `/lmrh/registry/{name}`     | Admin-only, soft-delete a registered dim (cluster-replicated tombstone, v3.0.29+) |

### Run runtime endpoints (v3.0.0+, joint contract with coordinator-hub)

Server-mediated agent loop. Replaces black-box `claude --print` invocations with state-machine-driven, observable, recoverable execution. Full operator reference in [`docs/runs-runbook.md`](docs/runs-runbook.md); wire spec in [`runs.openapi.json`](runs.openapi.json).

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/runs` | Create a Run (idempotent on `idempotency_key`, 24h TTL) |
| GET | `/v1/runs/{id}` | Get current state (status, tokens, current tool_use, etc.) |
| POST | `/v1/runs/{id}/cancel` | Cancel (idempotent; broker fans `cancelled` to live SSE consumers within ~10ms) |
| POST | `/v1/runs/{id}/tool_result` | Post a tool result back into a run |
| GET | `/v1/runs/{id}/events` | SSE event stream OR `?since_ms=` polling |
| POST | `/v1/runs/{id}/adopt` | Peer takeover after owner-node failure (R5) |

State machine: `queued → running → requires_tool → running → … → completed | failed | expired | cancelled`

### Admin / observability

| Method | Path | Description |
|--------|------|-------------|
| GET/POST | `/api/providers` | List / add / edit / soft-delete providers |
| GET/POST | `/api/keys`      | List / create API keys (with bulk-delete) |
| GET/POST | `/api/users`     | List / create users; per-user timezone + 24h preference |
| PATCH    | `/api/auth/preferences` | Update logged-in user's display prefs |
| GET      | `/api/monitoring/status`   | Provider health + circuit breaker states |
| GET      | `/api/monitoring/activity` | Activity log (paginated, searchable) |
| GET      | `/api/monitoring/activity/stream` | SSE live activity stream |
| GET      | `/api/monitoring/metrics?hours=24` | Per-provider rollup (request count, success rate, latency, cost, tokens) |
| GET      | `/cluster/status`          | Cluster node status |
| POST     | `/cluster/circuit-breaker/{id}/reset` | Force-close a circuit breaker |
| POST     | `/cluster/sync`            | Receive sync push from peer (HMAC) |

## Configuration

All settings can be edited live on the Settings page; environment variables are the defaults.

### Selected env vars

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | `change-me-in-production` | Session signing key |
| `DATABASE_URL` | `sqlite+aiosqlite:////app/data/llmproxy.db` | DB path |
| `LOG_LEVEL` | `info` | Logging level |
| `CIRCUIT_BREAKER_THRESHOLD` | `3` | Failures before opening circuit |
| `CIRCUIT_BREAKER_TIMEOUT_SEC` | `60` | Seconds circuit stays open |
| `HOLD_DOWN_SEC` | `120` | Post-failure provider hold-down |
| `RUNS_MAX_TURNS_CEILING` | `50` | Run runtime: max-turns admin ceiling (hard cap 200) |
| `RUNS_MAX_MODEL_CALLS_PER_MINUTE` | `5` | Per-Run rate limit |
| `KEEPALIVE_PROBE_INTERVAL_SEC` | `300` | Keep-alive probe interval (0 disables) |
| `ACTIVITY_LOG_RETENTION_DAYS` | `30` | Daily prune of activity_log + run_events + provider_metrics |
| `CLUSTER_ENABLED` | `false` | Enable cluster mode |
| `CLUSTER_NODE_ID` / `CLUSTER_NODE_URL` / `CLUSTER_PEERS` | — | Cluster identity + peers |
| `CLUSTER_SYNC_SECRET` | — | HMAC shared secret |
| `SMTP_ENABLED` | `false` | Enable email alerts |

## Routing

### LMRH protocol — semantic routing by task / cost / latency / modality

```
LLM-Hint: task=reasoning, cost=economy, region=us, exclude=codex-oauth;require
```

Append `;require` to any affinity to make it a hard constraint (returns 503 with the specific unmet dimension if it can't be satisfied). Response carries `LLM-Capability` with the chosen provider and model. Unknown dimensions surface in the `X-LMRH-Warnings` response header (v3.0.25+) with `register-at:/lmrh/register spec:/lmrh.md` discovery hints.

**Built-in dims:** `task`, `safety-min`, `safety-max`, `refusal-rate`, `region`, `latency`, `cost`, `context-length`, `modality`, `max-ttft`, `max-cost-per-1k`, `effort`, `cascade`, `hedge`, `tenant`, `freshness`, `exclude`, `provider-hint`. Runtime registration via `POST /lmrh/register` extends the canonical name space with collision-resolved `-2`/`-3` suffixes; the registry replicates across cluster peers.

**Hard model-family vs provider-type filter (v3.0.26+):** when a request pins a specific model (`claude-*`, `gpt-*`, `gemini-*`, etc.), the proxy filters candidates to provider types that can physically serve that family. Empty intersection → clean 503 instead of silent substitution.

**Embedding-on-chat rejection (v3.0.27+):** requests for embedding model names (`embed-*`, `text-embedding-*`) on `/v1/chat/completions` or `/v1/messages` return HTTP 400 pointing to `POST /v1/embeddings`.

### Sort modes — OpenRouter-parity model slug suffixes

| Suffix | Meaning |
|---|---|
| `:floor` | Cheapest provider that meets the request shape |
| `:nitro` | Lowest-latency provider |
| `:exacto` | Strict-precision provider (highest safety tier) |

### Auto-routing

Send `model: "auto"` (or `"llmp-auto"`) and let LMRH ranking pick provider AND model. Auto-task classifier infers a task dimension from the prompt before scoring.

## Failover + reliability

| Feature | Behavior |
|---|---|
| **Per-call hard deadline** | `asyncio.wait_for(connect=10s, read=60s)` wraps every upstream call. `ConnectTimeout` / `ReadTimeout` / `asyncio.TimeoutError` → immediate fail-over to next provider. |
| **Circuit breaker** | Three states (closed / open / half-open); auth-failure breaker has 24h hold (Anthropic OAuth-revocation pattern); billing breaker 1h. |
| **Provider failover** | Reuses ranked candidate list; `try_ranked_non_streaming` walks the chain on non-retriable failures. claude-oauth providers excluded from non-OAuth dispatch paths. |
| **Hedged requests** | Tail-at-scale pattern — fires backup on primary TTFT > 1.2× provider's p95. Token-bucket rate-limited (`HEDGE_MAX_PER_SEC`). |
| **Cluster stickiness (R5)** | Run-targeted endpoints redirect (307) to the run's `owner_node_id`. Debounced state replication via `/cluster/sync`; terminal pushes sync-acked. `POST /v1/runs/{id}/adopt` for owner-failure handoff. |
| **Recovery sweep** | On startup, scan for in-flight runs owned by this node + spawn workers from persisted message history. Emits `run_recovered` event. |

## Provider types

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
| `codex-oauth` | OAuth bearer (JWT) + `ChatGPT-Account-ID` | **OpenAI Codex CLI / ChatGPT subscription** — see below |

### Claude Pro Max subscription (`claude-oauth`)

Add a Claude Pro Max account as a provider without needing an Anthropic API key. In the Providers UI, pick `provider_type: claude-oauth` and click **Generate Auth URL**. Open the URL in a browser where you're signed in to claude.ai, approve, and paste the `CODE#STATE` string from the success page back into the form. The proxy handles the PKCE token exchange and stores access + refresh tokens (Fernet-encrypted at rest). Auto-refresh on 401 (one-shot, then surface).

Traffic to `claude-oauth` providers bypasses litellm and hits `platform.claude.com/v1/messages` directly with the Claude Code header bundle. Excluded from `/v1/chat/completions` routing (the OpenAI-format endpoint can't dispatch Anthropic-format upstream). Excluded from cascade / critique / hedging internal pipelines (those use litellm; OAuth needs the dedicated handler).

### OpenAI Codex CLI / ChatGPT subscription (`codex-oauth`, v3.0.16+)

Add a ChatGPT Plus / Pro / Team / Enterprise subscription as a provider without an `sk-…` API key. In the Providers UI, pick `provider_type: codex-oauth` and click **Generate Auth URL**. Open the URL in a browser where you're signed in to ChatGPT, approve, and paste the resulting `http://localhost:1455/auth/callback?code=…&state=…` URL from your browser's address bar back into the form. The browser will dead-end at that URL — that's expected; we just need the URL string for the code+state extraction.

Traffic to `codex-oauth` providers bypasses litellm and is dispatched to `chatgpt.com/backend-api/codex/responses` (OpenAI Responses API, **not** Chat Completions). The proxy translates Chat Completions ↔ Responses in both directions, including SSE streaming, so existing OpenAI-compatible clients (Cursor, aider, OpenAI Python/JS SDK with custom `base_url`, your bots) work unchanged. Always upstreams `stream: true` (the Codex backend rejects non-streaming) and accumulates if the caller asked for non-streaming.

Available models depend on subscription tier — fetch `GET /api/providers/{id}/scan-models` after creating the provider; Plus typically sees `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.2`, `codex-auto-review`. **Rate-limit awareness (v3.0.16+):** the proxy reads `x-codex-*` headers on every successful response (plan tier, primary 5h window % used, secondary weekly window % used, reset-at), and force-opens the CB on a 429 / limit-exceeded with a hold-down equal to the upstream's reset-after value. So when you hit the cap, traffic transparently fails over to the next provider until the subscription window resets — no manual intervention.

Both OAuth provider types share **cluster refresh-race recovery (v3.0.18)**: when two nodes refresh the same OAuth provider's access_token within the 60s sync window, the loser used to get `invalid_grant` (Anthropic and OpenAI both rotate the refresh_token on every call) and tripped a 24h auth-failure breaker. Now the loser fans out a signed `GET /cluster/oauth-pull/{provider_id}` to peers, adopts the freshest non-expired tokens it finds, and continues serving traffic. Only raises (back to the existing CB path) if no peer has fresher tokens — i.e. real upstream revocation.

## Observability

- **Activity log** — every request → `activity_log` table; bodies captured (request + response, up to 50KB each, scrubbed of secrets); searchable, paginated, SSE-streamable. Auto-pruned daily (default 30 days).
- **Per-provider metrics** — 5-min bucketed `provider_metrics` table; surface on Metrics page (sortable columns) and inline on the Providers page (24h chips on each row).
- **Keep-alive probes** — every enabled provider gets a `Hi from <ProviderName>` synthetic call every 5 min (configurable). Tagged `[probe]` in activity log. Includes `claude-oauth` and `codex-oauth` (v3.0.19+) via dedicated OAuth dispatch paths — neither speaks litellm-via-OpenAI-API, so probes go to the right upstream with the right headers.
- **OTEL spans** — GenAI semantic conventions (`gen_ai.operation.name`, `gen_ai.provider.name`, etc.) plus gateway extensions (`gen_ai.routing.lmrh_hint`, `gen_ai.run.id`, etc.). OTLP HTTP exporter when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; no-op otherwise.
- **Prometheus** — `/metrics` exposes per-provider request duration, token counts, TTFT, hedge attempts/wins, circuit-breaker state gauge.

## Cluster mode

All 3 nodes sync users + API keys + providers + system settings + run state via HMAC-authenticated `/cluster/sync` POST calls. SQLite uses **WAL mode** (v3.0.3) so concurrent writers don't lock the DB. Each node maintains independent provider configurations; spending caps are enforced cluster-wide via per-peer cost tracking.

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

# Tests
python -m pytest tests/unit -q                      # 800+ unit tests
python -m pytest tests/integration -q               # ~60 integration tests against live deployment
playwright install chromium                         # for the playwright UI suite
python -m pytest tests/integration/test_playwright_ui.py -v
```

## Rebuild + deploy (single node)

Per the project's docker rules — single container, never `down`:

```bash
cd /home/dblagbro/docker
sudo docker compose build llm-proxy2
sudo docker compose up -d --force-recreate --no-deps llm-proxy2
curl -s https://www.voipguru.org/llm-proxy2/health | jq
```

### Rolling-deploy caveat (v3.0.11+)

Provider rows now carry a `last_user_edit_at` timestamp set only by admin-facing edits. During a rolling deploy from a pre-v3.0.11 release, a v3.0.11+ node will **reject** edits replicated from a still-old peer when the local row has a `last_user_edit_at` and the peer's payload doesn't. This is intentional — without the stamp we can't tell a real edit on the peer apart from an OAuth auto-refresh or other background mutation, so we keep the local edit. Convergence resumes once both nodes are on v3.0.11+; if a real edit is lost during the window, re-do it on the upgraded node.

## Key differences from v1

| Feature | v1 (Node.js) | v2 (Python) |
|---|---|---|
| Runtime | Node.js + Express | Python 3.13 + FastAPI |
| DB | JSON files | SQLite (async, WAL) |
| Frontend | Server-rendered EJS | React 19 SPA |
| Auth | express-session | HTTP-only cookies + bcrypt |
| URL path | `/llmProxy/` | `/llm-proxy2/` |
| Port (internal) | 3000 | 3000 |
| Routing | priority + retry | LMRH semantic + circuit breaker + hedging + Run runtime |

## Documentation

- [`docs/runs-runbook.md`](docs/runs-runbook.md) — operator runbook for the Run runtime
- [`runs.openapi.json`](runs.openapi.json) — OpenAPI spec for `/v1/runs/*` endpoints (joint contract with coordinator-hub)
- [`docs/claude-pro-max-oauth-capture.md`](docs/claude-pro-max-oauth-capture.md) — OAuth capture flow notes
- [`docs/draft-blagbrough-lmrh-00.md`](docs/draft-blagbrough-lmrh-00.md) — LMRH 1.1 IETF draft (self-extension protocol)
- [`docs/lmrh-1.1-announcement.md`](docs/lmrh-1.1-announcement.md) — cross-project announcement of LMRH 1.1 features for consumer teams
- [`CHANGELOG.md`](CHANGELOG.md) — version history
