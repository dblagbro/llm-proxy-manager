# llm-proxy-v2 Architecture

## Overview

FastAPI proxy that accepts Anthropic (`/v1/messages`) and OpenAI (`/v1/chat/completions`)
requests, routes them to the best available upstream provider via litellm, and returns
responses in the caller's expected wire format.

## Module Map

```
app/
├── main.py                  FastAPI app factory, startup hooks, router registration
├── config.py                Pydantic Settings (env vars + .env)
├── config_runtime.py        Hot-reloadable settings (editable via Admin UI / cluster sync)
│
├── api/
│   ├── messages.py          POST /v1/messages — Anthropic wire format handler
│   ├── completions.py       POST /v1/chat/completions — OpenAI wire format handler
│   ├── image_utils.py       Image detection + stripping for both wire formats
│   ├── apikeys.py           CRUD + spending-cap/rate-limit for API keys
│   ├── providers.py         CRUD + model capability management for providers
│   └── admin.py             Admin auth, user management, settings UI API
│
├── auth/
│   ├── keys.py              API key verification, sliding-window rate limiting
│   └── admin.py             bcrypt password hashing, admin session handling
│
├── routing/
│   ├── router.py            Provider selection — returns RouteResult
│   ├── lmrh.py              LLM-Request-Hint parsing + CapabilityProfile dataclass
│   └── circuit_breaker.py   Per-provider open/half-open/closed state + hold-down
│
├── cot/
│   ├── pipeline.py          Chain-of-Thought iterative refinement pipeline
│   └── tool_emulation.py    Tool-use emulation for non-native providers:
│                              prompt building, message normalisation, parsing,
│                              Anthropic + OpenAI SSE/JSON response generators
│
├── cluster/
│   ├── manager.py           Peer state, heartbeat loop, push-sync outgoing
│   └── sync.py              apply_sync() — incoming peer data merge; peer cost tracking
│
├── monitoring/
│   ├── helpers.py           record_outcome() — shared success/failure metrics recorder
│   ├── metrics.py           request/token/cost DB writes
│   ├── pricing.py           litellm cost estimation
│   ├── status.py            provider health registration + status aggregation
│   ├── activity.py          activity feed / recent-request log
│   └── notifications.py     alert hooks (Slack, webhook)
│
└── models/
    ├── db.py                SQLAlchemy ORM models
    └── database.py          Async engine, session factory, migration runner
```

## Key Data Flows

### Incoming request
1. FastAPI extracts API key → `verify_api_key()` checks spending cap + rate limit
2. `parse_hint()` interprets `LLM-Hint` header into a `RouteHint`
3. `select_provider()` picks best provider: filters by capability (vision, tools, not-excluded),
   checks circuit breakers, ranks by priority, builds `RouteResult`
4. Endpoint applies image stripping (if `route.vision_stripped`) and extra kwargs
5. Dispatches to tool-emulation path, CoT path, or direct litellm call
6. `record_outcome()` centralises all metrics recording after the response

### Cluster sync
- Push: `push_sync()` in `manager.py` serialises local DB and POSTs to each peer every 60s
- Apply: `apply_sync()` in `sync.py` merges incoming users/keys/providers/settings;
  tracks per-peer key costs in `_peer_key_costs` for global spending-cap enforcement

## Key Types

| Type | Location | Purpose |
|------|----------|---------|
| `RouteResult` | `routing/router.py` | Provider selection output; carries litellm model, kwargs, flags |
| `CapabilityProfile` | `routing/lmrh.py` | Per-model capability descriptor (tasks, modalities, cost_tier, etc.) |
| `ApiKeyRecord` | `auth/keys.py` | Lightweight auth result passed through request lifecycle |
| `PeerNode` | `cluster/manager.py` | Peer state: URL, status, latency, last heartbeat |

## Extension Points

- **New provider type**: add row to DB, optionally add `infer_capability_profile()` case in `lmrh.py`
- **New routing criterion**: extend `RouteHint` in `lmrh.py`, filter in `select_provider()` in `router.py`
- **New wire format**: add endpoint file in `api/`, add image utils to `image_utils.py`, add SSE generators to `cot/tool_emulation.py`
- **New metric**: update `record_outcome()` in `monitoring/helpers.py` — propagates to all 6 call-sites automatically
