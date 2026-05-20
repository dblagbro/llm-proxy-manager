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
│   ├── messages.py              POST /v1/messages — Anthropic wire format handler:
│   │                              preflight, routing, cache, litellm/CoT dispatch
│   ├── _messages_streaming.py   SSE generators: _stream_cot_anthropic / _stream_anthropic /
│   │                              _stream_claude_oauth / _complete_claude_oauth /
│   │                              _webhook_completion_anthropic (extracted 2026-04-23)
│   ├── _messages_dispatch.py    Dispatch orchestration (v3.10.9): dispatch_claude_oauth_chain
│   │                              walks the claude-oauth provider chain (streaming /
│   │                              non-streaming + 401-refresh fallback); _select_excluding
│   │                              chain-walk helper. Extracted from messages.py's
│   │                              ~913-line messages() handler.
│   ├── completions.py           POST /v1/chat/completions — OpenAI wire format handler
│   │                              v3.0.38: claude-oauth providers reachable here via
│   │                              the wire-format translator (was excluded by v2.8.11)
│   ├── _completions_streaming.py Tail: _stream_cot_openai / _stream_openai /
│   │                              _webhook_completion_openai (extracted 2026-04-23)
│   ├── _oauth_chat_translate.py OpenAI Chat Completions ↔ Anthropic Messages
│   │                              format translation (v3.0.38). Lets
│   │                              /v1/chat/completions reach claude-oauth
│   │                              providers without client-side rewrites.
│   │                              openai_request_to_anthropic /
│   │                              anthropic_response_to_openai /
│   │                              stream_anthropic_to_openai_sse
│   ├── _request_pipeline.py     Shared preflight helpers (2026-04-23):
│   │                              apply_privacy_filters (guard+PII),
│   │                              build_hint_with_auto_task (parse + classify),
│   │                              apply_context_compression (truncate/mapreduce),
│   │                              build_base_response_headers,
│   │                              select_provider_with_503 + resolve_auto_model_into_body
│   │                              (v3.1.0: extracted shared provider-selection block
│   │                              — closes the v3.0.99 divergence-bug class where
│   │                              /v1/messages + /v1/chat/completions silently
│   │                              diverged on model_override plumbing).
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
│   ├── providers.py             Core provider CRUD (list/get/create/update/delete + usage)
│   ├── provider_lifecycle.py    Lifecycle ops (v3.9.8): clear-auth-failure, toggle,
│   │                              release-manual-overrides, test, scan-models
│   ├── provider_capabilities.py Capability admin (v3.9.8): list/upsert/infer + _serialize_cap
│   │                              (v3.1.0: was 1136 lines; OAuth flow endpoints
│   │                              moved to providers_oauth.py — now 875 lines)
│   ├── providers_oauth.py       claude-oauth + codex-oauth authorize / exchange /
│   │                              rotate (v3.1.0: extracted from providers.py).
│   │                              Six endpoints over a parameterized
│   │                              OAuthProviderSpec (CLAUDE_OAUTH_SPEC /
│   │                              CODEX_OAUTH_SPEC). Adding a third OAuth provider
│   │                              type (Vertex, Azure-AD, Bedrock) is now ~30
│   │                              lines instead of a 200-line copy-paste.
│   └── admin.py                 Admin auth, user management, settings UI API
│
├── auth/
│   ├── keys.py              API key verification (rate-limit state re-exported from rate_limit_state)
│   ├── rate_limit_state.py  In-process RPM / RPD / burst state + check primitives (extracted 2026-04-23)
│   └── admin.py             bcrypt password hashing, admin session handling
│
├── routing/
│   ├── router.py                  Provider selection — returns RouteResult;
│   │                                build_litellm_model, build_litellm_kwargs (public helpers);
│   │                                resolve_chat_model_for_provider (v3.0.32, shared
│   │                                embedding→chat fallback used by keepalive + scanner)
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
│   ├── sync.py              apply_sync() — incoming peer data merge orchestrator; peer cost tracking
│   ├── sync_handlers.py     Per-table _apply_<table> handlers (v3.9.8 P5 refactor):
│   │                          _apply_blocked_ips, _apply_ai_reviews,
│   │                          _apply_provider_ai_reviews, _apply_caller_memory,
│   │                          _apply_caller_memory_markers, _apply_external_usage_snapshots
│   └── auth.py              HMAC signing/verification primitives (sign_payload, verify_payload,
│                              verify_cluster_request, auth_headers_for)
│
├── memory/                  Proxy-side caller memory (#267, shipped 2026-05-14)
│   ├── store.py             Redis hot cache + SQLite king-store + in-process fallback
│   │                          (mirrors cot/session.py pattern); cluster-replicated via LWW
│   ├── inject.py            Phase 4 — request-time injection as system-prompt prefix.
│   │                          Gated on X-Conversation-Id header (Q1 locked 2026-05-14)
│   ├── extract.py           Phase 5 — Anthropic memory-tool write-back from response
│   │                          tool_use blocks (memory_20250818)
│   ├── flush.py             Phase 6 — registry-based per-vendor flush dispatcher;
│   │                          detects provider transitions, emits best-effort cleanup
│   └── recover.py           Phase 7 — registry-based back-pressure recovery;
│                              reconstructs missing content from upstream when marker survives
│
├── monitoring/
│   ├── helpers.py           record_outcome() — shared success/failure metrics recorder
│   ├── metrics.py           request/token/cost DB writes
│   ├── pricing.py           litellm cost estimation
│   ├── status.py            provider health registration + status aggregation
│   ├── activity.py          activity feed / recent-request log
│   └── notifications.py     alert hooks (Slack, webhook)
│
├── models/
│   ├── db.py                SQLAlchemy ORM models
│   └── database.py          Async engine, session factory, migration runner
│
└── utils/                   Shared cross-cutting helpers (added v3.0.33)
    └── timefmt.py           utc_iso(dt) — appends Z to ISO output so JS
                              `new Date(...)` correctly converts UTC →
                              user's tz preference. Used by every
                              user-facing isoformat() callsite (v3.0.33).
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

**What syncs cluster-wide vs what stays node-local** (read before
diagnosing a "9/10 on one node, 9/10 on another" pattern — the two
counts often have a common cause that is itself cluster-synced):

| Surface | Scope | Notes |
|---|---|---|
| `users`, `api_keys`, `providers` (incl. `extra_config`), `system_settings` | **synced** | merged by `apply_sync()` every 60 s; this is why `providers.extra_config.bridge_url` resolves to the same value on every node |
| Circuit-breaker state (`provider_id` → `open/half-open/closed`, hold-down) | **synced** | a single node's upstream failures **propagate the CB state to every other node**. Operational consequence: one node thrashing a provider degrades every node's view of that provider; conversely, "9/10 healthy providers" observed on a quiet node often points to a problem somewhere *else* on the cluster (this exact pattern surfaced BUG-025 from c1conv during the v4.3.2 post-deploy QA). Intentional trade-off: fleet-wide CB visibility vs node-local CB isolation. |
| Per-peer key costs (`_peer_key_costs`) | **synced** | needed for global spending-cap enforcement |
| `activity_log` rows | **node-local — NOT synced** | each row's `event_meta.node_id` names the node that wrote it. A node's `/api/activity` endpoint returns only that node's history; cross-node activity comparison requires querying each node separately. Asymmetry with CB state above is intentional (rows are high-volume; sync overhead would dominate) but worth noting when triaging cluster-wide patterns. |
| Per-process state: rate-limit counters (`auth/rate_limit_state.py`), cache (`api/_cache_inject.py`), provider scan results in flight | **node-local** | by construction — process memory. |

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
- **New chat-style probe path** (test button, smoke job, etc.): call `resolve_chat_model_for_provider()` instead of reading `provider.default_model` directly — keeps embedding-defaulted providers (Cohere) from misrouting (see v3.0.27/30/31 bug history → v3.0.32 extraction)
- **New user-facing endpoint that returns timestamps**: import `utc_iso` from `app.utils.timefmt` instead of bare `dt.isoformat()` — the `Z` suffix tells JavaScript to convert from UTC instead of treating as local time (v3.0.33)
- **New LMRH built-in dim**: add a case branch in `app/routing/lmrh/score.py`, the dim name to `_builtin_dim_names()` in `app/api/lmrh.py`, and document in `docs/draft-blagbrough-lmrh-00.md`. The middleware + cluster sync need no changes
- **New rolling-window aggregate**: extend `get_provider_rolling_windows()` in `app/monitoring/metrics.py` with another conditional sum — single SQL pass covers all windows
- **Wire-format translator for a new provider type**: pattern is `app/api/_oauth_chat_translate.py` (v3.0.38) — request shape inversion + non-streaming response inversion + SSE delta-chunk re-emission. Hook into the chat handler's provider-type branch alongside `codex-oauth` / `claude-oauth`
- **New OAuth-flow provider type** (Vertex, Azure-AD, Bedrock, etc.): add a flow module under `app/providers/<name>_oauth_flow.py` exposing `start_authorize() / extract_code_from_callback() / exchange_code() / OAuthFlowError`. Then in `app/api/providers_oauth.py` add a `<NAME>_OAUTH_SPEC = OAuthProviderSpec(...)` constant and three endpoint stubs (`authorize`, `exchange`, `rotate`) that pass the spec into `_do_authorize` / `_do_exchange_create` / `_do_rotate`. ~30 lines total, no logic duplication.

## Design contract

`design.md` is the contract for module boundaries, layering rules, when-to-refactor heuristics, and observability/cluster invariants. Read it before any non-trivial refactor or new module.

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

## grok.com web subscription (`grok-web` — v3.2.0+)

The third "subscription as a provider" path, after `claude-oauth` and
`codex-oauth`. Replays grok.com's browser-side request shape against
`https://grok.com/rest/app-chat/conversations/{id}/responses` so an
operator's Lite / Premium subscription serves traffic without a paid
xAI API key.

Two operating modes — both configured via `Provider.extra_config`:

```
              ┌─────────────────────────────┐
caller ──→ proxy ──→ grok-web dispatcher ─┬──→ MANUAL: HTTP replay using
                                          │           pasted cookies + headers
                                          │           on Provider row
                                          │
                                          └──→ BRIDGE: POST to grok_bridge
                                                       sidecar /api/chat
                                                       (sidecar holds live
                                                        Playwright session)
```

```
app/providers/grok_web.py    (743 lines as of v3.2.8)
├── _is_bridge_mode(extra_config)  bridge_url present → bridge path
├── _validate_extra_config         enforces required fields per mode
├── _build_headers / _build_body / _flatten_messages_to_prompt   shared shape
├── _bridge_chat                   forward to bridge /api/chat
├── complete_grok_web              non-streaming OpenAI-shape result
├── stream_grok_web                streaming SSE (OpenAI delta chunks)
├── stream_grok_web_anthropic      streaming SSE (Anthropic event format)
└── anthropic_response_from_openai response shape conversion

SUPPORTED_MODELS = ["grok-3", "grok-4",
                    "x-ai/grok-3", "x-ai/grok-4"]

  v3.2.8: includes both bare and OpenRouter-style slugs so caller
  requests like `x-ai/grok-4` capability-match grok-web at priority=1
  instead of falling through to OpenRouter (per-call billing).

MODEL_TO_MODE_ID:
  grok-3 / x-ai/grok-3 → modeId="fast"
  grok-4 / x-ai/grok-4 → modeId="expert"
```

**Conversation reuse**: `POST /conversations/new` is rejected by Cloudflare
anti-bot from server IPs. Operator supplies one existing conversation_id
(any chat in their account); each proxy call sends `parentResponseId: ""`
so callers don't share thread context inside that conversation. The
conversation grows in the operator's grok.com UI over time.

## grok-bridge sidecar (`grok_bridge/` — v3.2.1+)

Separate Docker service `llm-proxy2-grok-bridge` that maintains a
logged-in grok.com session via Playwright + Chromium in a headed Xvfb
display, exposed to the operator via noVNC.

```
grok_bridge/
├── Dockerfile           mcr.microsoft.com/playwright/python:v1.45.0-jammy
│                          + xvfb + x11vnc + websockify + novnc + supervisord
├── supervisord.conf     boots Xvfb → fluxbox → x11vnc → websockify in order
├── start.sh             waits for Xvfb, then exec's uvicorn (FastAPI app)
└── app.py               FastAPI control plane:
                           /healthz                 alive check
                           /api/status              login state + cookie freshness
                                                      (PUBLIC — no token; surfaces
                                                       only booleans + counts so
                                                       the /login HTML can render)
                           /api/login/start         drive Chromium to grok.com
                           /api/conversation/new    POST /conversations/new
                                                      (gated by token)
                           /api/chat                inference surface called by
                                                      llm-proxy2's grok-web
                                                      dispatcher; X-Bridge-Token
                                                      enforced
                           /login                   noVNC-framed HTML for the
                                                      operator's one-time sign-in
                           /vnc/*                   websockify+noVNC HTML5 client
```

**Persistence**: `/data/playwright-state` is a Docker volume mounted into
the bridge. Playwright's `launch_persistent_context` writes cookies +
localStorage there, so a container restart preserves the operator's
signed-in session. The 25-minute background refresh loop visits
grok.com so Cloudflare passively reissues `__cf_bm` / `cf_clearance`
before they expire.

**nginx routing** (in `/home/dblagbro/docker/config/nginx/nginx.conf`):

```
/grok-bridge/api/chat   → bridge:8443  (no auth_request — bridge enforces
                                         X-Bridge-Token internally so peer
                                         llm-proxy2 nodes can reach it via
                                         the public URL)
/grok-bridge/vnc/*      → bridge:6080  (websockify + noVNC; auth_request)
/grok-bridge/*          → bridge:8443  (login, /api/status, /api/login/start;
                                         auth_request gates against
                                         /api/auth/me — operator's
                                         llmproxy_session cookie required)
/grok-bridge-auth-check → llm-proxy2:3000/api/auth/me  (internal; subrequest)
@bridge_unauthorized    → 302 /llm-proxy2/?bridge_login_required=1
```

**Sidecar topology — there is exactly ONE grok-bridge in the fleet.**
The `llm-proxy2-grok-bridge` container runs **only on tmrwww01**; there
is no per-node sidecar. Every node — including tmrwww01 itself —
addresses it through the **public URL**
(`https://www.voipguru.org/grok-bridge/...`), because the source of truth
is `providers.extra_config.bridge_url`, and provider records cluster-sync
across the fleet. tmrwww01 hairpins through its own public nginx →
back into the local bridge container; tmrwww02 + c1conv reach it across
the open internet. This is by design (cookies + Cloudflare passive
refresh live in one volume, so one shared session is correct), but it
has three operational consequences worth knowing:

1. **No per-node auth state.** A re-login restores grok-web for *every*
   node simultaneously, but a bridge outage breaks grok-web for *every*
   node simultaneously. There is no graceful per-node degradation.
2. **The CB state for grok-web is cluster-synced** (see §"Cluster sync"
   above), so a single bridge failure trips the breaker on every node —
   compounding consequence #1.
3. **"Read the live provider config before targeting a sidecar fix."**
   Because `bridge_url` is a public URL, a `_local_sidecar_reachable()`
   helper that does a docker-internal hostname probe will trivially
   succeed and gate nothing. The v4.3.2 release shipped exactly this
   dead code (BUG-026) — diagnosed only after deploying. A 30-second
   `SELECT extra_config FROM providers WHERE id='<grok-web>'` would
   have prevented the misship.

The v4.4 arc (per-node bridge auth) is the planned redesign of this
layer — see `docs/remediation-plan.md` §5 Batch C.

**Live test**: `tests/integration/` (added v3.2.x) covers the bridge
contract; manual OAuth login is operator-driven (one-time per fresh
volume).

### v4.4 dormant per-node-bridge scaffolding

v4.4 attempted a Path A redesign — per-node bridge container on each
of the 3 proxy hosts, each holding its own logged-in Chromium for
the same Grok account. The 2026-05-20 live spike found grok.com
enforces single-account-session semantics: a second concurrent IP
got silently de-authed; a third got "You have been blocked" before
login completed. Path A was rejected; Path B (single shared bridge,
hardened) is the operative topology. See
`docs/4.4-per-node-bridge-design.md` §3.2 for the spike data.

What stays in the codebase as **dormant scaffolding** (no behavior
change without an explicit operator opt-in):

- **`ProviderNodeAuthState` table** (`app/models/db.py`) — composite
  PK `(provider_id, node_id)`; cluster-synced LWW on `last_check_at`;
  each node writes its own rows.
- **`app/routing/node_auth_state.py`** — `write_local_state` upsert,
  `read_state` / `read_all_states`, `is_local_node_routable` gate.
- **`app/monitoring/keepalive.py`** grok-web probe branch — best-
  effort writes a row on every probe outcome (mapping success→ok,
  auth→needs_reauth, network/timeout/5xx→bridge_down, else→
  needs_reauth).
- **`app/routing/router.py:select_provider()`** — per-node filter
  triggered ONLY by `extra_config.node_local_session=True`. No-op
  for every provider in production today.
- **`app/routing/circuit_breaker.py:_persist_auto_skip()`** — exempts
  node_local_session-tagged providers from the cluster-wide auto-
  skip path. Same no-op without the flag.
- **`GET /api/providers/{id}/node-auth-states`** + the React
  `NodeBridgeStatusPanel` — returns/renders the per-node states.
  Renders only when `node_local_session=True` is set on the
  provider's extra_config (so the panel is invisible today on
  every provider).

If a future Grok policy change makes Path A viable, flipping
`node_local_session=True` on the grok-web provider's `extra_config`
+ updating `bridge_url` to a per-host docker-internal name +
restoring the per-node bridge containers (image
`dblagbro/llm-proxy2-grok-bridge:v4.4-rc1` on Docker Hub) activates
the entire pipeline. No code change required for the flip.

Until then: M-1 image hardening (the `xdpyinfo` Xvfb readiness
probe + compose `/healthz` healthcheck) is the durable production
win — BUG-025 is mechanically prevented in Path B too.

## LMRHv2 — bidirectional metrics feedback (v3.3.0+)

LMRH 1.x was one-way: client sends `LLM-Hint` → proxy decides. v2
adds a feedback channel where the proxy publishes provider/model
cost/latency/reliability/circuit data and clients use it to construct
optimal hints for their next request.

```
app/routing/lmrh/snapshot.py    LmrhSnapshot dataclass + 30s background
                                  refresh loop. Per-node, no cluster sync
                                  of the snapshot itself. ETag derived from
                                  identity-affecting fields.

app/api/lmrh_v2.py              5 endpoints + per-key sliding-window rate
                                  limit + ETag conditional GET handling.
                                  Feature-gated via lmrh_v2_enabled
                                  (default False — endpoints return 404
                                  when off).

  Endpoints (all under /llm-proxy2/):
    GET /.well-known/lmrh-config    public; protocol metadata + endpoint
                                      discovery (RFC 8615)
    GET /lmrh/providers             auth; live snapshot, key-scoped,
                                      ETag-cacheable, 30s max-age
    GET /lmrh/providers/{id}        auth; single-provider deep view
    GET /lmrh/quotes?model=X        v3.3.1+; pre-flight a request,
                                      returns ranked candidates without
                                      dispatching. Reuses
                                      select_provider(dry_run=True).
    GET /lmrh/health                auth; aggregate fleet counters

app/main.py                     Link header injection middleware
                                  (RFC 8288). Every /v1/* response
                                  carries:
                                    Link: </.well-known/lmrh-config>; ...
                                    Link: </lmrh/providers>; ...
                                    LMRH-Version: 2.0  (or 1.2 default-off)

sdk/python/lmrh_client.py       Single-file Python SDK reference.
                                  Background polling, ETag-aware,
                                  graceful 404 degradation. build_hint()
                                  synthesizes RFC 8941-shaped hints from
                                  caller preferences (cheapest / fastest /
                                  most_reliable / model_family / region).
```

**Per-cluster vs per-node flag**: `lmrh_v2_enabled` lives in the
cluster-synced `SystemSetting` table, so flipping on one node
propagates to peers. Isolated nodes (`CLUSTER_ENABLED=false`, e.g.
the smoke instance) stay off until manually flipped.

**Rate limits** (operator decision #5): per-key, default 4/min for
`/lmrh/providers` and 60/min for `/lmrh/quotes`. Override via
`ApiKey.lmrh_polling_rpm` / `lmrh_quotes_rpm` columns (NULL = use
default). State is in-memory per-process (`_rate_state` dict in
`lmrh_v2.py`); when budgets tighten or callers scale, move to Redis.

**Scope filter** (operator decision #1): every endpoint applies
`Provider.owned_by_key_id` filtering at render time so callers see
only providers their key can route to. Operator-private providers
stay private.

**Backward compat**: every LMRH 1.x client keeps working unchanged.
v2 adds optional response headers (`Link`, `LMRH-Version`,
`LMRH-Hint-Echo`) and new endpoints; nothing in the existing surface
changes. Coordinator-hub team got 1-week notice via KB #2520 before
the protocol-version flag flipped fleet-wide.

**Design + decisions doc**: `project_lmrhv2_design.md` in operator
memory. Operator-locked answers to all 7 design questions on
2026-05-09.
