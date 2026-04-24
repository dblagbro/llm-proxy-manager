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
│   ├── oauth_capture/           Multi-vendor OAuth capture package (v2.5.0; packaged 2026-04-24):
│   │   ├── __init__.py          merges sub-routers; re-exports test helpers
│   │   ├── presets.py           CapturePreset + 8-entry PRESETS table
│   │   ├── profiles.py          /_presets + /_profiles/… CRUD endpoints
│   │   ├── logs.py              /_log + SSE tail + NDJSON export
│   │   ├── passthrough.py       /{profile}/{path} forwarding catch-all
│   │   └── serializers.py       header filters + row→dict + _safe_text
│   │                            (sidecar/terminal.py deleted in v2.7.0 —
│   │                             replaced by claude-oauth provider flow)
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
│   ├── prompts.py           PLAN_SYSTEM_VERBOSE/COMPACT, CRITIQUE/REFINE/RECONCILE/VERIFY_SYSTEM
│   │                          extracted from pipeline.py (2026-04-24)
│   ├── verify.py            resolve_verify + run_verify_pass extracted from pipeline.py (2026-04-24)
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

## Claude Pro Max OAuth (`claude-oauth` provider type, v2.7.1+)

Instead of managing an Anthropic API key, admins can attach their Claude
Pro Max subscription as a provider. The flow is entirely in-browser —
no CLI install, no paste-a-token step.

```
app/providers/
├── claude_oauth.py           Credential parser, build_headers(model=),
│                              _beta_flags_for_model (strips 1M-context
│                              flag for Haiku which Pro Max doesn't grant).
└── claude_oauth_flow.py      PKCE authorize URL builder, code exchange,
                              refresh_access_token, refresh_and_persist.

app/api/_messages_streaming.py
  _complete_claude_oauth /    Bypass litellm — POST directly to
  _stream_claude_oauth          platform.claude.com with Bearer auth.
  _inject_claude_code_system  Prepend "You are Claude Code..." marker
                                required by the OAuth endpoint (otherwise
                                returns masked rate_limit_error).
```

**OAuth wire flow** (v2.7.2 endpoints extracted from
`@anthropic-ai/claude-code` v2.1.119 binary):

```
Admin clicks "Generate Auth URL"
  → POST /api/providers/claude-oauth/authorize
  → Proxy builds PKCE + state and returns
      https://claude.com/cai/oauth/authorize
        ?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e
        &response_type=code
        &redirect_uri=https://platform.claude.com/oauth/code/callback
        &scope=org:create_api_key user:profile user:inference
               user:sessions:claude_code user:mcp_servers user:file_upload
        &code_challenge=<S256>&code_challenge_method=S256&state=<state>
  → Admin opens URL, approves on claude.ai
  → Anthropic success page displays "CODE#STATE" with a Copy button
Admin pastes CODE#STATE back into the form
  → POST /api/providers/claude-oauth/exchange
  → Proxy POSTs JSON to platform.claude.com/v1/oauth/token
      {grant_type: "authorization_code", code, state, client_id,
       redirect_uri, code_verifier}
  → Token response → stored on Provider row
      (api_key = access_token, oauth_refresh_token, oauth_expires_at)
```

**Request-side quirks** (all handled in `_complete_claude_oauth` /
`_stream_claude_oauth`):

- `Authorization: Bearer sk-ant-oat01-…` (not `x-api-key`)
- `anthropic-beta: claude-code-20250219, oauth-2025-04-20,
  context-1m-2025-08-07, interleaved-thinking-…, …`
  — the full CC beta bundle; `context-1m-2025-08-07` is stripped for Haiku.
- `x-app: cli`, `anthropic-dangerous-direct-browser-access: true`
- `system` must start with one of three allowed Claude Code markers or
  the API returns a masked `rate_limit_error`. `_inject_claude_code_system`
  prepends the base marker (`"You are Claude Code, Anthropic's official
  CLI for Claude."`) unless caller already identifies as CC.

**Token rotation**: Anthropic rotates the refresh_token on each use.
`refresh_and_persist(provider, db)` is the canonical helper — it refreshes
AND writes the rotated token back to the DB. Never call
`refresh_access_token()` directly from production paths; the rotated token
would be dropped and the next refresh would fail with `invalid_grant`.

**Live test**: `scripts/test_claude_oauth_live.py` exercises 17 code paths
(basic, streaming, multi-turn, tool_use, vision, caching, concurrent,
multiple models, scan, test button, refresh, invalid-model errors,
metrics) against a real provider. Not run in CI — opt-in via docker exec.
