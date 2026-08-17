# llm-proxy-v2 Architecture

## Overview

FastAPI proxy that accepts Anthropic (`/v1/messages`), OpenAI (`/v1/chat/completions`)
and OpenAI Responses (`/v1/responses`) requests, routes them to the best available
upstream provider via litellm, and returns responses in the caller's expected wire
format. v5.0.0 (2026-06-04) added a per-key + system-wide compliance enforcement layer
that can block configured companies (10 built-in + operator-defined customs) at the
provider-routing, model-family, and client-product layers — with audit-grade event
logging, daily integrity hash chain, and cluster-quorum policy push.

**Current version: 5.21.8 (2026-07-16)**

### Ship history since v5.0.7 (arc-end summary, doc-drift refresh)

- **v5.6.x–v5.7.x** — MCP surface: aggregation endpoint, per-key allow/deny + token budget, capability scout, streaming tool injection, MCP dashboard panel
- **v5.8.x** — AI Integration Protocol (`/announce`, `/api/integration/chat`); OAuth flows (Cursor + Codex) hardened
- **v5.9.x** — Audio (TTS/STT) + Images endpoints; disconnect-watchdog rolled to responses/audio/images/cluster-sync
- **v5.10–v5.11** — MCP capability back-pressure closed loop; NVIDIA NIM provider; `/api/requests/stream` SSE + `/api/admin/requests/detail/{id}` companion (v5.20.5)
- **v5.13–v5.14** — Cursor OAuth in-band refresh; callback registry (built-in hook: X-Compliance-Substitution)
- **v5.15.x** — Per-account OAuth fan-out (schema + admin endpoints)
- **v5.16** — Consolidated `x-llmproxy-config` policy header
- **v5.20.x — refusal detection arc**: detection module + prompt hardening + cascade retry + self-update endpoint + hooks-override header + LiteLLM model-cost catalog + `self_edit_permissions` admin UI
- **v5.21.x — refuse-tolerance arc**: LMRH `refuse-tolerance` dim + buffered streaming cascade + hotfix (v5.21.1) + per-key default injection + heartbeat streaming mode + AIRI prompt-cue classifier + hint SSE + frontend badge
- **v5.21.6–v5.21.8 — DB pool leak permanent fix**: root-caused chronic 24-48h exhaustion outage. See "DB pool leak diagnostic path" section below.
- **v5.22.x — node-wedge arc (the real root cause)**: the v5.21.x pool work treated symptoms; this
  arc found the cause. `v5.22.4` closed a DB **connection-hold** leak (chat handlers held `get_db`'s
  open read-txn across the upstream call and the whole stream → `await db.commit()` release-boundary
  before dispatch). `v5.22.5` fixed an aiosqlite **OS-thread** leak with a fixed non-churning pool
  (`max_overflow=0`, `pool_pre_ping=False`, `pool_size=50`, `recycle=-1`) — aiosqlite runs one thread
  per connection, and any connection created-then-destroyed or GC'd-not-closed orphans its thread
  forever. **`v5.22.6` found the actual root cause**: `app/routing/fallback.py::_next_route` excluded
  only one provider per `select_provider` call and "progressed" by re-adding an id already in the
  exclusion set — a no-op — so the loop spun forever at ~2 DB queries per provider per pass. A single
  `/v1/messages` request hitting a provider error pegged the event loop and drained the pool to 50/50
  on an idle node. Fix: pass the cumulative `exclude_provider_ids` set; defensive guard raises rather
  than looping. Pin: `tests/unit/test_v5226_next_route_terminates.py`. **This retired the standing
  "single-event-loop CPU ceiling" hypothesis — the node was spinning, not saturated.**
- **v5.22.7–v5.22.11 — account/auth surface**: password reset (admin + self-service email), Cohere
  tool-shape fix, grok-bridge noVNC sub-path + honest `healthz`, Google/OIDC SSO, sign-in by email
  address, and `users.email` replication across the cluster.

## DB pool leak diagnostic path (v5.21.6–v5.21.8)
> Superseded as a root-cause account by the v5.22.x arc above — the pool exhaustion described
> here was a downstream symptom of the `_next_route` infinite loop fixed in v5.22.6. Kept because
> the diagnostic method (pool tracing, thread counting, py-spy sampling) is still the right playbook.

**Symptom class**: pool utilization creeps up over ~24-48h until 50 base + 100 overflow = 150 sessions all leaked → `/api/auth/login` blocks at DB checkout → admin UI dies → operator has to notice + trigger a container recreate.

**Root cause pattern (chronic since 2026-07-09)**: any endpoint declaring `db: AsyncSession = Depends(get_db)` AND returning a `StreamingResponse` holds the request-scoped session for the **entire stream lifetime**. Long-lived SSE consumers (LMRH stream, run event stream, admin activity tail) held sessions for hours per browser tab. Multi-tab load compounded to exhaustion.

**Confirmed leak sites, now fixed**:
- `runs.py::get_events` (v5.21.7) — SSE stream that can run for **hours** while a long job executes
- `lmrh_v2.py::stream_snapshot` (v5.21.8) — infinite-loop SSE for LMRH snapshot pushes

**Diagnostic infrastructure**:
- **SIGUSR2 handler** (`app/monitoring/pool_trace_signal.py`, v5.21.6): `docker kill --signal=SIGUSR2 llm-proxy2` dumps the current `_async_session_traces` dict to logs. No login required — breaks the chicken/egg where the admin trace endpoint needs login and login needs the pool.
- **Auto-watcher** (`app/monitoring/pool_leak_watcher.py`, v5.21.7): background task polls pool utilization every 30s; auto-dumps trace when 50/75/90% thresholds are first crossed (armed → fired → re-armed on drop below).
- **CI static-grep pin** (`tests/unit/test_v5218_streaming_pool_leak_pin.py`, v5.21.8): fails if any handler combines `= Depends(get_db)` with `StreamingResponse(` without a `pool-leak-audit: <reason>` comment documenting why it's safe. Recognized reasons: `rows-materialized` (rows loaded before stream returned), `watchdog+bounded` (disconnect watchdog + bounded stream duration), `exempt-<reason>` (case-by-case).

**Belt-and-suspenders**: daily 04:00 auto-recycle cron on all 3 hosts, chained with `docker exec nginx nginx -s reload`. Nginx caches upstream IPs across container recreate; without the reload, external HTTPS returns 502 even though the container is healthy.

## What we lead on (vs LiteLLM Proxy + Portkey Gateway, as of 2026-06-30)

Surfaced from hub-team's 2026-06-30 peer-comparison-roadmap research (19 verified claims, 3-0/2-1 adversarial check). These are llm-proxy-v2 primitives with **no OSS equivalent** in the peers:

1. **`X-Compliance-Substitution` response header convention.** Always emitted on 2xx relay responses (v5.9.3, v5.14.0 hook). Three values: `true` (substituted), `false` (policy evaluated, no substitution needed), `pass-through` (no per-key policy). Hub-side defense-in-depth scanners treat absence as a hard error. Neither LiteLLM nor Portkey ship a substitution-rationale header — Portkey's documented response headers (`x-portkey-{trace-id, retry-attempt-count, cache-status, last-used-option-index}`) encode routing outcome only; LiteLLM's `x-litellm-model-{id, group}` is a partial Resolved-Model signal with no rationale companion (worse: GitHub #22709 reports LiteLLM overwrites `response.model` with the client-requested alias, working *against* substitution transparency).

2. **`compliance_events` row-per-request audit table.** Never-purged, integrity-chain-hashed (`compliance_audit_chain`), cluster-replicated. LiteLLM ships audit logging as Enterprise-license-only ($250-$30k/yr per TrueFoundry pricing reviews) AND only captures admin/management CRUD on four entity types (Keys/Teams/Users/Models × Create/Update/Delete/Regenerate) — **not** per-request substitution or compliance events. Portkey: no documented per-request audit at this depth. The OSS-tier per-request audit row is a genuine llm-proxy-v2 differentiator.

3. **Path A / Path B MCP distinction.** Path A = explicit `/mcp/` aggregation endpoint (Apache-2.0 FastMCP); Path B = auto-inject MCP tools into `/v1/messages` requests when the caller's `mcp_tools_allow` policy permits. The per-request header-layer split is unique in the 2026-06-30 research pass. Portkey's MCP Gateway (Apache-2.0 OSS since March 2026) controls access via API-key → server/tool config mappings but has no equivalent dual-path semantics.

What we *aspire* to that peers have today: hierarchical RBAC (Portkey 4-tier — backlog v5.15+), consolidated policy header (`x-llmproxy-config` style — backlog v5.16+).

## Module Map

```
app/
├── main.py                  FastAPI app factory, startup hooks, router registration
├── config.py                Pydantic Settings (env vars + .env)
├── config_runtime.py        Hot-reloadable settings (editable via Admin UI / cluster sync)
│
├── api/
│   ├── messages.py              POST /v1/messages — Anthropic wire format handler:
│   │                              preflight, routing, cache, litellm/CoT dispatch.
│   │                              v5.0.9: compliance orchestration extracted to
│   │                              _compliance_handler (-166 LOC; 1095 → 929).
│   │                              v5.7.18: pre-route setup extracted to
│   │                              _messages_pre_route (-42 LOC; 1180 → 1138).
│   ├── _messages_pre_route.py   v5.7.18/v5.7.19: pre-route helpers extracted
│   │                              from messages.py. ``prepare_request_context``
│   │                              (sub-block 1) bundles verify_api_key + tenant
│   │                              ctx + raise_if_banned_client_ua +
│   │                              raise_if_llm_emergency_stopped + caller-memory
│   │                              telemetry. ``normalize_request_body``
│   │                              (sub-block 2) bundles input validation +
│   │                              suffix parsing + embedding-on-chat guard +
│   │                              model:"auto" + alias resolution.
│   │                              ``translate_to_openai_if_needed`` (sub-block 3)
│   │                              is the v3.10.0 widened Fix B Anthropic→OpenAI
│   │                              translation block. Phase 1 of the refactor
│   │                              proposal complete; messages.py 1180 → 1063.
│   │                              Proposal: docs/refactor-proposal-2026-06-17.md.
│   ├── _messages_streaming.py   SSE generators: _stream_cot_anthropic / _stream_anthropic /
│   │                              _stream_claude_oauth / _complete_claude_oauth /
│   │                              _webhook_completion_anthropic (extracted 2026-04-23)
│   ├── _messages_dispatch.py    Dispatch orchestration (v3.10.9): dispatch_claude_oauth_chain
│   │                              walks the claude-oauth provider chain (streaming /
│   │                              non-streaming + 401-refresh fallback); _select_excluding
│   │                              chain-walk helper. v4.4.38 added try_cascade_dispatch
│   │                              (cheap-route → grader verdict → accept-or-escalate)
│   │                              — the cascade orchestration sub-block of the
│   │                              non-streaming else-branch that messages.py used to
│   │                              inline. messages.py 927 → 861 LOC.
│   ├── _compliance_handler.py   v5.0.9: the four compliance orchestration sites
│   │                              messages.py + completions.py used to mirror inline.
│   │                              raise_if_banned_client_ua (451 path) /
│   │                              raise_for_no_substitute_exception (503 / no-local 503) /
│   │                              emit_substitution_disclosure_for_route (200 disclosure) /
│   │                              disclosure_headers_for_upstream_error (502 disclosure).
│   │                              Every v5.0.x patch touched both handlers in lockstep
│   │                              before this extraction — net -166 LOC × 2.
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
│   │                              v4.4.39 (UI clarity): Providers list page renders
│   │                                priority as ordinal ("12th priority" not
│   │                                "priority 12"); preferred badge renamed
│   │                                ✓ preferred → 🥇 router's pick today; form
│   │                                label disambiguates from the badge.
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
│   │                                select_provider strategy + scoring + capability fit.
│   │                                v4.4.38: litellm-binding helpers (build_litellm_*,
│   │                                PROVIDER_TYPE_TO_LITELLM, PROVIDER_DEFAULT_MODELS,
│   │                                resolve_chat_model_for_provider, _is_embedding_model,
│   │                                _model_family_provider_types, _native_thinking_params)
│   │                                moved to litellm_binding.py and re-exported here.
│   │                                998 → 800 LOC.
│   │                                v4.4.35: cursor-oauth added to PROVIDER_TYPE_TO_LITELLM
│   │                                + base_url allowlist (fixed the v4.4.31..v4.4.34
│   │                                "Incorrect API key" mystery — litellm was
│   │                                routing to api.openai.com without api_base).
│   │                                v4.4.40 (BUG-086): cursor-oauth added to BOTH
│   │                                the claude-* AND gpt-* branches of
│   │                                _model_family_provider_types — the family filter
│   │                                was eliminating cursor providers before priority
│   │                                ordering applied, so claude-haiku requests were
│   │                                skipping priority-4 Cursor in favor of
│   │                                priority-7 Anthropic-OAuth. Caught by operator
│   │                                routing-bug report.
│   ├── external_rotation.py       Auto-skip rule + multi-vendor preferred-pick.
│   │                                v3.7.4: claude-oauth utilization-weighted
│   │                                preference. v4.4.41: generalized to
│   │                                reorder_subscription_by_utilization(provider_type=…);
│   │                                back-compat reorder_claude_oauth_by_utilization
│   │                                now reorders BOTH claude-oauth AND cursor-oauth
│   │                                in one pass (router's existing single callsite
│   │                                gets multi-vendor preferred-pick free).
│   │                                evaluate_rules_for_all_providers query covers
│   │                                both subscription types since v4.4.41.
│   ├── litellm_binding.py         v4.4.38: provider_type → litellm prefix tables,
│   │                                build_litellm_model + build_litellm_kwargs,
│   │                                embedding-default → chat fallback. Each new
│   │                                subscription-provider type (claude-oauth,
│   │                                codex-oauth, grok-web, cursor-oauth, …) adds
│   │                                ~3 lines here; select_provider strategy
│   │                                code stays untouched.
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
│   ├── sync.py              apply_sync() — incoming peer data merge orchestrator; peer cost
│   │                          tracking. v5.0.10: api_keys + providers extracted; sync.py
│   │                          1024 → 573 LOC (under 800 trigger).
│   ├── sync_handlers.py     Per-table _apply_<table> handlers (v3.9.8 P5 refactor):
│   │                          _apply_blocked_ips, _apply_ai_reviews,
│   │                          _apply_provider_ai_reviews, _apply_caller_memory,
│   │                          _apply_caller_memory_markers, _apply_external_usage_snapshots,
│   │                          _apply_compliance_events, _apply_compliance_policy_changes
│   │                          (v5.0.0), _apply_api_keys, _apply_providers (v5.0.10)
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
├── resources/               v5.23 — local accelerator telemetry (read-only).
│   │                          Resource admission, not MCP capability back-pressure.
│   │                          LOCAL_ACCEL_ENABLED=false is a complete no-op.
│   ├── probe.py             NVML / nvidia-smi / RAM / Ollama GET /api/ps snapshot
│   └── (later)              residency, admission, queue, lifecycle
│
├── monitoring/
│   ├── helpers.py                  record_outcome() — shared success/failure metrics recorder
│   ├── metrics.py                  request/token/cost DB writes
│   ├── pricing.py                  litellm cost estimation
│   ├── status.py                   provider health registration + status aggregation
│   ├── activity.py                 activity feed / recent-request log
│   ├── notifications.py            alert hooks (Slack, webhook)
│   ├── anthropic_billing_worker.py v3.7.0: 4h periodic scrape of Anthropic Console
│   │                                 usage per claude-oauth provider; writes
│   │                                 ExternalUsageSnapshot rows; feeds
│   │                                 external_rotation auto-skip rule.
│   ├── codex_billing_worker.py     v3.7.27 (#245): same shape as Anthropic worker
│   │                                 for ChatGPT Plus / Codex Cloud.
│   └── cursor_billing_worker.py    v4.4.41: same shape as Anthropic worker for
│                                     cursor-oauth providers. Uses Provider.api_key
│                                     (the stored WorkosCursorSessionToken) directly
│                                     as a Cookie header — no separate credential
│                                     plumbing. Polls cursor.com/api/usage-summary +
│                                     /api/dashboard/get-aggregated-usage-events.
│                                     Live-deploy gotcha: hit apex cursor.com (not
│                                     www.cursor.com) — httpx strips Cookie across
│                                     subdomain redirects.
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

## Compliance enforcement (`app/compliance/` — v5.0.0+)

Driven by a US Government policy requirement on one deployment that bans Anthropic
products entirely (the Claude CLI itself, the `@anthropic-ai/claude-code` package,
all Anthropic SDKs). Built generic so any company can be banned by any deployment.

### Decision authorities

- **CADC** (claude-alternative-design-committee) — 3rd-party spec authority. Locked
  the architecture across 33 decisions; canonical spec in `docs/5.0-compliance-design.md`,
  taxonomy in `docs/compliance-taxonomy-v5.0.0.md`, impact map in
  `docs/5.0-impact-map.md`.
- **llm-proxy2 team** — implements + audits.
- **Coordinator Hub team** — operates the bot fleet that consumes the proxy. Their
  v2.1.0+ ships hub-side enforcement as defense-in-depth (using the
  `/api/admin/policy-snapshot` endpoint shipped in v5.0.7).

### Six layers of enforcement

1. **UA pre-check (decision 16+22)** — HTTP 451 if the request's `User-Agent` matches
   a banned client product pattern. Fires BEFORE any provider routing. Narrow patterns
   only (`claude-cli/`, `anthropic-sdk-python/`, `@anthropic-ai/claude-code`, etc.);
   case-insensitive; no bare-substring matching that would false-positive on docs.
2. **Provider pre-filter (decision 11+14)** — router drops providers whose
   `owner_company` is banned, OR whose `default_model` family lineage is banned
   (Bedrock-Anthropic = both `aws` and `anthropic` per `model_family_companies`).
3. **Cross-family substitution** — surviving providers serve a non-banned model;
   the route flags `compliance_substituted=True` and the seven `X-Compliance-*`
   headers + optional SSE prelude disclose the swap.
4. **No-compliant-substitute (decision 4)** — if the pre-filter empties the candidate
   pool, HTTP 503 with `X-Compliance-Refusal-Reason: no-compliant-provider-available`
   (or `no-compliant-local-provider` for the `coordinator-local` logical alias path,
   added v5.0.4).
5. **Cache + memory filter (decision 7+18)** — `Provider.owner_company` propagates to
   `caller_memory.source_company` and the semantic cache index. Read paths filter rows
   whose `source_company` is banned (NULL is treated as banned — unknown provenance
   can't be served to a banned key).
6. **Allowed-paths middleware (decision 21)** — per-key `allowed_paths` (JSON list,
   exact match). Drops anything not on the list with HTTP 403
   `X-Compliance-Reason: path-not-in-allowed_paths`. Has a debug-echo bypass for
   sandbox keys with `debug_echo_enabled=True`.

### Package layout

```
app/compliance/
├── __init__.py         Public exports — what callers actually import
├── company_map.py      KNOWN_COMPANIES (10-entry taxonomy) + provider_type → company
│                       and model-family → company resolvers (model_family_to_company
│                       returns first match; model_family_companies returns ALL
│                       matches — Bedrock-Anthropic returns {anthropic, aws}).
├── policy.py           get_effective_blocklist (api_key ∪ system, 30s cache);
│                       filter_providers (raises ComplianceNoSubstituteError on empty);
│                       ComplianceNoLocalProviderError subclass (v5.0.4 F-fix).
├── ua_detect.py        detect_client_company(ua) — case-insensitive pattern match
│                       across KNOWN_COMPANIES ∪ custom; returns (company_id, pattern,
│                       product_label).
├── disclosure.py       7-header builder, SSE prelude (Anthropic event + OpenAI
│                       first-frame inject), Accept-Compliance-Events opt-in.
└── audit.py            emit_event (ComplianceEvent row), emit_policy_change
                        (CompliancePolicyChange + cluster fan-out), daily integrity
                        hash chain (ComplianceAuditChain), retention purge.
```

### Schema additions (v5.0.0)

3 new tables, 6 new columns:

- `compliance_events` — one row per substitution / refusal / cache or memory filter /
  path-not-allowed. Append-only; cluster-replicated via append-only sync handler with
  audit_id dedup.
- `compliance_policy_changes` — one row per operator-initiated policy edit.
  Mandatory `reason` field (decision 6). Records cluster fan-out outcome
  (`applied_to_peers`, `pending_peers`, `cluster_sync_status`).
- `compliance_audit_chain` — daily SHA-256 hash chain (decision 10). Chain links
  forward (`prior_day_chain_hash + sorted_event_content`). Computed by
  `app/monitoring/compliance_audit_worker.py` 90 min after boot then every 24h.
- New columns: `api_keys.blocked_companies`, `api_keys.allowed_paths`,
  `api_keys.debug_echo_enabled`, `providers.owner_company`,
  `caller_memory.source_company`, `caller_memory_marker.source_company`.

### Public admin/user endpoints

- `GET /api/me/compliance` — per-key transparency (effective blocklist, allowed_paths,
  24h substitution + 451 counts, last policy change).
- `GET /api/admin/cluster/compliance-ready` — preflight before flipping a policy
  (per-peer health + state-consistency).
- `GET /api/admin/compliance-events` — JSON or 11-column CSV stream (audit team).
- `GET /api/admin/compliance-policy-changes` — recent policy edits.
- `GET /api/admin/compliance-audit-worker` — daily worker snapshot + last 30 chain rows.
- `GET /api/admin/cursor-oauth-expiry` (v5.0.4) — cursor-oauth JWT expiry monitor.
- `GET /api/admin/policy-snapshot` (v5.0.7) — canonical taxonomy + UA patterns +
  system block list + drift-stable `policy_version` hash. Built for the Coordinator
  Hub team's v2.1.0 hub-side enforcement layer.
- `GET/POST /api/debug/echo-client` — sandbox echo (key.debug_echo_enabled gated).

### 4 logical model aliases (decision 29)

`coordinator-code`, `coordinator-fast`, `coordinator-reasoning`, `coordinator-local`
— stable identifiers the consumer team's CLI configs reference instead of vendor model
names. Resolved through the existing LMRH hint scorer, except `coordinator-local`
which applies a HARD `is_self_hosted_provider` filter outside the scorer (ollama /
vllm / llamacpp / lmstudio / localai + `compatible+self_hosted=true` + operator-tagged
`owner_company in ('internal','local','self-hosted')`).

### Daily workers

- `app/monitoring/compliance_audit_worker.py` (v5.0.2) — computes the prior-day
  integrity hash + purges events older than `compliance_audit_retention_days`
  (default 2555 = 7 years).
- `app/monitoring/cursor_oauth_expiry_monitor.py` (v5.0.4) — decodes JWT exp on each
  cursor-oauth provider, backfills `oauth_expires_at` when NULL, alerts at <14d.
  Lays the groundwork for the noVNC-replacement refresh-flow once v4.4.37's
  refreshToken probe captures an empirical token from a fresh re-auth.

### v5.0.x patch history (2026-06-04)

- **5.0.1** — `Provider.owner_company` auto-derivation hook + one-shot startup
  backfill; X-Compliance-* headers preserved on upstream-error 502 responses.
- **5.0.2** — daily compliance audit worker (integrity hash chain + retention purge);
  `/api/admin/compliance-audit-worker` admin endpoint.
- **5.0.3** — `/v1/responses` translation shim (Responses-shape ↔ ChatCompletions);
  streaming returns 501 (full SSE translation deferred to v5.1).
- **5.0.4** — F-anomaly fix: `coordinator-local` without a self-hosted provider now
  returns 503 with `X-Compliance-Refusal-Reason: no-compliant-local-provider`;
  cursor-oauth JWT expiry monitor.
- **5.0.5** — cluster sync `apply_sync` chunked into per-section commits. Fixed
  a slow-degradation SQLite write-lock contention where sync apply time walked from
  1.5s to 19.6s over 10h soak, hitting the 10s busy_timeout and locking out
  concurrent writers.
- **5.0.6** — audit + disclosure record the caller's ORIGINAL requested model
  (`_orig_request_model` captured before the v3.0.36 cross-family-fallback body
  rewrite). Pre-v5.0.6 the `requested_model` audit field captured the served model.
- **5.0.7** — `GET /api/admin/policy-snapshot` for the Coordinator Hub team's
  hub-side enforcement layer.

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
app/providers/grok_web.py    (825 lines as of v4.4.38)
├── _is_bridge_mode(extra_config)  bridge_url present → bridge path
├── _validate_extra_config         enforces required fields per mode
├── _build_headers / _build_body / _flatten_messages_to_prompt   shared shape
├── _bridge_chat                   v4.4.38: moved to grok_web_bridge.py;
│                                    re-exported here for back-compat. First
│                                    step of the manual/bridge axial split
│                                    (the v3.10.9 next-target list). Future
│                                    work converts to a grok_web/ package
│                                    with manual.py + bridge.py + shared.py.
├── complete_grok_web              non-streaming OpenAI-shape result (branches manual/bridge)
├── stream_grok_web                streaming SSE (OpenAI delta chunks)
├── stream_grok_web_anthropic      streaming SSE (Anthropic event format)
└── anthropic_response_from_openai response shape conversion

app/providers/grok_web_bridge.py   (76 lines — v4.4.38)
└── _bridge_chat                   POST → bridge /api/chat (with X-Bridge-Token).
                                     Lazy-imports GrokWebError / GrokWebAuthError /
                                     _map_upstream_status / _pick_conversation_id
                                     from grok_web to avoid a load-time circular.

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
