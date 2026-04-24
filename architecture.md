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
│   ├── messages.py              POST /v1/messages — Anthropic wire format handler (routing + cache only)
│   ├── _messages_streaming.py   Tail: _stream_cot_anthropic / _stream_anthropic /
│   │                              _webhook_completion_anthropic (extracted 2026-04-23)
│   ├── completions.py           POST /v1/chat/completions — OpenAI wire format handler
│   ├── _completions_streaming.py Tail: _stream_cot_openai / _stream_openai /
│   │                              _webhook_completion_openai (extracted 2026-04-23)
│   ├── _request_pipeline.py     Shared preflight helpers (2026-04-23):
│   │                              apply_privacy_filters (guard+PII),
│   │                              build_hint_with_auto_task (parse + classify),
│   │                              apply_context_compression (truncate/mapreduce),
│   │                              build_base_response_headers
│   ├── oauth_capture.py         Multi-vendor OAuth capture platform (v2.5.0) — profiles,
│   │                              SSE tail, NDJSON export, 7 CLI presets
│   ├── models.py                GET /v1/models — OpenAI-compatible model listing
│   ├── image_utils.py           Image detection + stripping for both wire formats (deduped 2026-04-23)
│   ├── apikeys.py               CRUD + spending-cap/rate-limit for API keys
│   ├── providers.py             CRUD + model capability management for providers
│   └── admin.py                 Admin auth, user management, settings UI API
│
├── auth/
│   ├── keys.py              API key verification (rate-limit state re-exported from rate_limit_state)
│   ├── rate_limit_state.py  In-process RPM / RPD / burst state + check primitives (extracted 2026-04-23)
│   └── admin.py             bcrypt password hashing, admin session handling
│
├── routing/
│   ├── router.py                  Provider selection — returns RouteResult;
│   │                                build_litellm_model, build_litellm_kwargs (public helpers)
│   ├── lmrh/                      LMRH protocol package (split from lmrh.py on 2026-04-23):
│   │   ├── __init__.py            re-exports everything below
│   │   ├── types.py               HintDimension / LMRHHint / CapabilityProfile + weight tables
│   │   ├── parse.py               parse_hint: RFC 8941 parser w/ legacy fallback
│   │   ├── score.py               score_candidate / rank_candidates / rank_candidates_with_scores
│   │   └── headers.py             build_hint_set_header / build_capability_header
│   ├── capability_inference.py    Heuristic fallback: infer_capability_profile from model name
│   └── circuit_breaker.py         Per-provider open/half-open/closed state + hold-down
│
├── cot/
│   ├── pipeline.py          Chain-of-Thought orchestration — plan/draft/critique/refine loop;
│   │                          parse_cot_request_headers() shared by both endpoint handlers
│   ├── critique.py          Pure parsers + heuristics extracted from pipeline.py (2026-04-23):
│   │                          parse_score, parse_gaps, parse_critique, should_verify,
│   │                          INFRA_TOOLS, SHELL_CODE_BLOCK
│   ├── branches.py          Task-adaptive CoT branches extracted from pipeline.py (2026-04-23):
│   │                          run_summarize_branch, run_math_branch, run_code_branch
│   ├── tool_emulation.py    Tool-use emulation for non-native providers:
│   │                          prompt building, message normalisation, parsing, LLM call
│   ├── structured_output.py JSON-schema repair loop (Wave 5 #24)
│   ├── verify_exec.py       Reflexion verify-step parse/execute/grade
│   ├── session.py           Redis-backed CoT session store (in-memory fallback)
│   └── sse.py               Wire format serialization — Anthropic + OpenAI SSE primitives,
│                              tool/text response generators, FINISH_TO_STOP, to_anthropic_response
│
├── cluster/
│   ├── manager.py           Peer state, heartbeat loop, push-sync outgoing
│   ├── sync.py              apply_sync() — incoming peer data merge; peer cost tracking
│   └── auth.py              HMAC signing/verification primitives (sign_payload, verify_payload,
│                              verify_cluster_request, auth_headers_for)
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
6. Response headers include `X-Provider`, `LLM-Capability`, `X-Resolved-Model` (litellm model string)
7. `record_outcome()` centralises all metrics recording + activity log after the response

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

- **New provider type**: add row to DB, optionally add `infer_capability_profile()` case in `routing/capability_inference.py`
- **New routing criterion**: extend `RouteHint` in `lmrh.py`, filter in `select_provider()` in `router.py`
- **New wire format**: add endpoint file in `api/`, add image utils to `image_utils.py`, add SSE generators to `cot/sse.py`
- **New metric**: update `record_outcome()` in `monitoring/helpers.py` — propagates to all 6 call-sites automatically
