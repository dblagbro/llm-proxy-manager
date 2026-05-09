# llm-proxy2 — architecture

**Version**: keeps pace with `app/__version__.py` (currently v3.5.0).
**Last updated**: 2026-05-09 (after the R1/R2 pipeline-helper extraction).

This document describes the static module layout and the runtime data
flow for the proxy. It pairs with [`refactor-log.md`](refactor-log.md)
which is the moving ledger of architectural changes.

## Top-level layout

```
app/
├── __version__.py        single source of truth for version (read by /health, /openapi.json, OTel tags)
├── config.py             Pydantic settings — env-loaded, immutable at runtime
├── config_runtime.py     mutable settings that admins flip via the UI (cluster-synced)
├── main.py               FastAPI app factory, route registration, lifespan hooks
├── api/                  HTTP endpoints (one file per logical surface)
├── auth/                 API-key resolution + RBAC + rate-limit state
├── budget/               per-key spending caps + cost recording
├── cache/                semantic cache (decide → check → store)
├── cluster/              multi-node sync + LWW conflict resolution
├── cot/                  Chain-of-Thought-Emulation pipeline
├── models/               SQLAlchemy ORM + db connection / migration list
├── monitoring/           activity log, provider metrics, keep-alive probes
├── observability/        Prometheus metrics + OpenTelemetry tracing
├── privacy/              prompt guard + PII masking
├── providers/            per-provider dispatch helpers + scanner (model catalog)
├── routing/              router (`select_provider`), LMRH parsing, hedging, circuit breaker, retries
├── runs/                 long-running session runtime (the "Runs" surface)
├── utils/                small leaf helpers (timefmt, etc.)
sdk/python/                reference Python SDK consumed by external callers
grok_bridge/               Playwright sidecar for grok.com web subscription
docs/                      this document, RFC, runbooks, LMRH spec
tests/                     unit + integration + SDK tests (1035+ passing)
```

## Module boundaries (the rules we enforce)

1. **`api/` is the only module that touches `fastapi`.** Everything below
   the API surface is framework-agnostic so we can run the same logic in
   tests, in the run worker, and in cluster-sync handlers.
2. **`models/` is the only module that defines SQLAlchemy table classes.**
   Other modules import the classes; nobody else declares `Column(...)`.
3. **`routing/` and `providers/` may not import from `api/`.** The
   request-pipeline goes one direction only. The exception is
   `api/_grok_web_dispatch.py` which sits in `api/` precisely because it
   needs to build FastAPI responses.
4. **`monitoring/` is allowed to import from `models/` and
   `routing/circuit_breaker` but NOT from `api/` or `providers/`.**
   It's a sink, not a participant in dispatch.
5. **`cluster/` only writes to `models/` tables tagged as
   cluster-replicated.** `activity_log` and `provider_metrics` are
   per-node; everything in `provider_capabilities`, `lmrh_dims`, etc. is
   cluster-synced.

## Request flow — `/v1/messages` and `/v1/chat/completions`

These two endpoints share ~80% of their orchestration. The shared
helpers live in [`app/api/_request_pipeline.py`](../app/api/_request_pipeline.py)
and are called in this order:

```
                    /v1/messages                /v1/chat/completions
                    (anthropic shape)           (openai shape)
                          │                            │
                          └────────┬───────────────────┘
                                   ▼
             apply_privacy_filters()       (guard + PII mask)
             build_hint_with_auto_task()   (parse LMRH + classify)
             apply_context_compression()   (truncate / map-reduce)
             build_base_response_headers()
             select_provider_with_503()    (router → 503 on no-fit)
             resolve_auto_model_into_body()
                                   │
                          ┌────────┴────────┐
                          ▼                 ▼
                 maybe_serve_from_cache()   (R1) — semantic-cache hit short-circuit
                 maybe_engage_cot()         (R2) — CoT-E branch when route.cot_engaged
                          │
                          ▼
                 [tool emulation │ hedging │ direct dispatch]
                 (still wire-format-specific in messages.py / completions.py)
                          │
                          ▼
                 record_outcome()  (activity log + metrics + circuit breaker)
                 maybe_store()     (semantic cache write-back)
```

### Why two endpoint files instead of one

Anthropic and OpenAI have legitimately different wire formats:

- **Tool calls**: Anthropic emits `tool_use` blocks; OpenAI emits
  `tool_calls` arrays.
- **Streaming SSE**: Anthropic events are `event: message_start /
  content_block_delta / ...`; OpenAI streams are `data: {choices: [{delta: ...}]}`.
- **System prompts**: Anthropic has a top-level `system` field; OpenAI
  puts system in `messages[0]`.
- **Image attachments**: different MIME-encoding conventions.

The orchestration AROUND those differences is identical. The R1/R2 split
is the right granularity — one helper per logical step, called from both
endpoints with shape-specific callbacks where needed.

## Provider dispatch

`app/routing/router.py:select_provider` is the heart of routing. It:

1. Builds the candidate list from enabled `Provider` rows.
2. Filters by ownership (`owned_by_key_id`), circuit-breaker state,
   capability match (model_id OR aliases — v3.4.1).
3. Applies LMRH hint scoring + capability-profile ranking.
4. Returns a `RouteDecision` with `provider`, `litellm_model`,
   `cot_engaged`, `tool_emulation_engaged`, etc.

Each provider type has a dispatch path:
- **claude-oauth / codex-oauth**: direct HTTPS to platform endpoints
  with OAuth bearer tokens (`app/providers/claude_oauth.py`,
  `app/providers/codex_oauth.py`). The `_complete_claude_oauth` /
  `_stream_claude_oauth` pair in `app/api/_messages_streaming.py`
  share request setup via `_prepare_claude_oauth_request()` + the
  `_CLAUDE_OAUTH_TIMEOUT` constant (R4, 2026-05-09); each then
  layers shape-specific dispatch (sync POST vs SSE streaming).
- **grok-web**: bridge sidecar (`grok_bridge/`) running Playwright
  against grok.com; proxy talks to bridge over HTTP
  (`app/providers/grok_web.py` + `app/api/_grok_web_dispatch.py`).
  The 3 manual-mode dispatch functions
  (`complete_grok_web` / `stream_grok_web` /
  `stream_grok_web_anthropic`) share request setup via
  `_build_manual_request(extra_config, prompt, model)` (R3,
  2026-05-09); each then layers format-specific httpx behavior
  (sync POST, OpenAI-stream wrapping, Anthropic-SSE wrapping).
- **everything else**: `litellm.acompletion()` with provider-specific
  `api_base` / headers.

## Observability

- **Activity log** (`app/monitoring/activity.py`): every dispatch
  outcome lands here with `event_type=llm_request` (user traffic) or
  `event_type=keepalive_probe` (synthetic — v3.3.4+).
- **Provider metrics** (`models/db.py:ProviderMetric`): 5-minute
  buckets, per-provider, used by LMRHv2 snapshot.
- **Circuit breaker** (`routing/circuit_breaker.py`): in-memory state,
  exported as Prometheus gauge.
- **OpenTelemetry**: `init_tracer` in `observability/otel.py` reports
  spans to whatever OTel collector is configured.

## Schema migrations

llm-proxy2 uses **idempotent ALTER-TABLE-on-startup** migrations in
`app/models/database.py:_run_migrations`. This is intentional — the
fleet hot-reloads on every container recreate, and Alembic's revision
graph adds operational overhead we don't need at the current scale
(~30 migrations to date). When the migration list crosses 50 entries
it'll be worth re-evaluating; that's tracked as a future item in
[refactor-log.md](refactor-log.md).

## Where to look first when…

| Question | File |
|---|---|
| "How does a request flow end-to-end?" | `app/api/messages.py` (anthropic) or `app/api/completions.py` (openai) |
| "Why was provider X chosen?" | `app/routing/router.py:select_provider` |
| "How is grok-web different?" | `app/providers/grok_web.py` + `app/api/_grok_web_dispatch.py` |
| "Is this a multi-route model?" | `app/routing/lmrh/snapshot.py` (family/variant in `_ModelSnap`) |
| "How does cluster sync work?" | `app/cluster/sync.py` |
| "Where do I add a new schema column?" | `app/models/db.py` (ORM) + `app/models/database.py` (migration ALTER TABLE) |
| "What CoT does this request run?" | `app/cot/pipeline.py` + `app/cot/branches.py` (task-adaptive) |
| "Where is the LMRHv2 wire format?" | `docs/lmrh-2.0-bidirectional.md` (spec) + `app/api/lmrh_v2.py` (impl) |

## Versioning + protocol

- `app/__version__.py` is the only place the proxy version string lives.
  All other readers (`/health`, OTel tags, `/openapi.json`) read from there.
- LMRH protocol version is independent: currently 2.1, advertised in
  `/.well-known/lmrh-config.supported_versions` alongside 1.2 + 2.0.
- Both versions follow loose semver: major = breaking, minor =
  additive feature, patch = fix.

## See also

- [`refactor-log.md`](refactor-log.md) — running ledger of architectural changes
- [`rfc/2026-05-model-identity.md`](rfc/2026-05-model-identity.md) — canonical model id + aliases + family/variant RFC
- [`lmrh-2.0-bidirectional.md`](lmrh-2.0-bidirectional.md) — LMRHv2 protocol spec
- `CHANGELOG.md` — per-version release notes
