# Changelog

All notable changes since v2.7.6. Older history available in `git log`.

The project follows [Semantic Versioning](https://semver.org/) loosely:
**major** = breaking API changes; **minor** = additive features; **patch** = fixes.

---

## v3.0.x — Run runtime, cluster ops, observability

### v3.0.7 — daily prune worker for activity_log + provider_metrics + run_events

Daily background sweep prunes rows older than `activity_log_retention_days` (default 30 days, admin-tunable). Batched DELETEs (5000 rows/batch) keep individual transactions short under WAL mode. Initial sweep delayed 1h post-boot.

### v3.0.6 — sortable metrics columns + per-provider 24h chips

- **MetricsPage:** all 6 columns (Provider / Requests / Success % / Avg Latency / Tokens / Cost) clickable to sort. Toggle direction by clicking the active column.
- **ProvidersPage:** 24h metrics chip inline on each provider card (`24h: N req · X% · Yms · N tok · $Z`); hidden when zero traffic. Sort-by selector at top: Priority, Name, Requests, Success rate, Latency, Cost.

### v3.0.5 — clean 503 on `/v1/messages` when all providers unavailable

Catches `RuntimeError("All providers are currently unavailable")` from `select_provider` and converts to a 503 with an actionable message naming the most-likely cause (Anthropic OAuth revocation → 24h breaker) and the fix (re-auth via UI). Same shape as the v3.0.4 fix on `/v1/chat/completions`. Triggered during cutover monitoring when GCP node's claude-oauth tokens were server-side revoked.

### v3.0.4 — clean 503 on `/v1/chat/completions` when no compatible providers

Catches `RuntimeError("No providers available after excluding types {'claude-oauth'}")` and converts to a 503 with a message naming the cause (claude-oauth providers can't dispatch through `/v1/chat/completions`) and the two valid resolutions (use `/v1/messages` OR enable a non-OAuth provider). Triggered during the v1-chain retirement window when only claude-oauth providers were enabled.

### v3.0.3 — SQLite WAL + busy_timeout fix

`PRAGMA journal_mode=WAL` (one-time, db-file-level) + `PRAGMA busy_timeout=10000` (per-connection via SQLAlchemy event listener) + `PRAGMA synchronous=NORMAL` (safe with WAL). Fixes `sqlite3.OperationalError: database is locked` under concurrent write load (cluster sync receivers + Run worker events + keep-alive probes + activity log all hitting the same file).

### v3.0.2 — keep-alive probes + pricing fix

- **Pricing:** previous `litellm.completion_cost(prompt_tokens=...)` API was rejected by current litellm with TypeError, silently falling through to $0.00 for everything. Switched to `litellm.cost_per_token`. Override table now matches bare model names (no provider prefix) so claude-oauth dispatched calls resolve correctly.
- **Keep-alive probes:** new `app/monitoring/keepalive.py` sweeps every enabled provider every 5 min (configurable; 0 disables). Per-provider unique prompt (`Hi from <ProviderName>`) so activity_log rows are distinguishable. Tagged `[probe]` + `probe: true` in metadata. Handles claude-oauth via the OAuth dispatch path.

### v3.0.1 — post-v3.0.0 regression fixes

- **Settings type drift** — four `SCHEMA` entries declared `type='str'` for fields the pydantic settings layer types as `float`. When a node inserted a SystemSetting row, `_coerce(value, value_type='str')` returned the raw string, and `settings.shadow_traffic_rate > 0` raised `TypeError: '>' not supported between instances of 'str' and 'int'` on every successful non-streaming `/v1/messages` call. Fixed: SCHEMA types corrected; `load()` now coerces using SCHEMA-declared type, not row-stored value_type (schema is authoritative).
- **`spending_cap_usd` sentinel** narrowed: `>= 0` (was `> 0`) so zero stays a hard block while `-1` clears.
- **`collect_sse` test helper** filters non-default-channel `data:` lines (was capturing `event: budget` heartbeat as a regular event).

### v3.0.0 — Run runtime (final)

Six-phase joint delivery with the coordinator-hub team. Server-mediated agent loop replacing black-box `claude --print` invocations.

- **R1** — Schema (`runs`, `run_messages`, `run_events`, `run_idempotency`) + pure FSM with 63 transition tests + stub endpoints + OpenAPI artifact + per-user UTC/timezone preferences
- **R2** — Worker (one `asyncio.Task` per Run) + hard per-call deadline (`asyncio.wait_for(connect=10s, read=60s)`) + `ConnectTimeout`/`ReadTimeout` → immediate fail-over (B.7 fix) + recovery sweep on startup with `run_recovered` events + 4 chaos tests
- **R3** — Context compaction at 80% threshold (cheapest haiku or `compaction_model` override) + tool spec translation (Anthropic↔OpenAI per provider's `native_tools` capability) + cancel-mid-tool-wait
- **R4** — In-memory event broker (1000-event ring per run, sub-100ms SSE) + `Last-Event-ID` resume + 15s keepalive + idempotency LRU cache
- **R5** — Cluster stickiness (307 redirect to owner node) + debounced state replication (250ms non-terminal, sync-acked terminal) + `POST /v1/runs/{id}/adopt` with 30s owner-grace
- **R6** — Per-Run rate limit (`runs_max_model_calls_per_minute=5` default) + 100-concurrent-runs load test + chaos suite + `docs/runs-runbook.md`

Joint smoke against v3.0.0-r4: 5/5 green.

---

## v2.9.x — UI polish + metrics page fix

### v2.9.1 — activity row inline req/resp previews
Each row now shows `→ <request preview>` + `← <response preview>` inline (240 chars each); error replaces response slot on failure. ~3 lines per row → 3 dense lines with inline meta.

### v2.9.0 — settings tooltips + metrics page fix
- `?` HelpHint icon next to every CoT-E / Native-Reasoning / Circuit-Breaker / Email-Alerts setting
- Metrics page un-broken: `get_all_provider_summary` had referenced `r.avg_ttft_ms` not in SELECT, 500'd silently, frontend rendered all zeros. Now aggregates ttft properly + shows provider names alongside IDs.

---

## v2.8.x — claude-oauth chain isolation, activity log payload capture

### v2.8.11 — exclude claude-oauth from `/v1/chat/completions`
OAuth providers were occasionally selected for OpenAI-format requests, surfacing as `Connection error.` upstream. Filter at routing.

### v2.8.10 — non-empty `error_str` + 300s OAuth non-stream timeout
`str(httpx.ReadTimeout())` was `""`, making activity_log show `error: null` for upstream timeouts. Added `_exc_str()` helper that falls back to exception class name. Bumped non-stream OAuth timeout from 60s → 300s for parity with streaming.

### v2.8.9 — three claude-oauth error patterns from activity log
Cache_control overflow (count existing markers, omit ours when total ≥ 4), default `max_tokens=4096`, internal-pipeline OAuth filter (`excluded_provider_types={"claude-oauth"}` on cascade cheap_route, CoT critique_route, hedging backup_route, grader_route).

### v2.8.8 — never run claude-oauth providers through litellm chain
Fallback chain skips OAuth providers; only the dedicated `_complete_claude_oauth` / `_stream_claude_oauth` handlers reach platform.claude.com.

### v2.8.7 — whitelist 1M-context flag
Older Sonnet/Opus snapshots 400'd on the 1M-context beta flag; now whitelisted per-model.

### v2.8.6 — two 502 root causes
`UnboundLocalError` on cache-miss path + OAuth chain falling into litellm dispatch. Fixed both.

### v2.8.5 — activity log: pagination, search, refresh, per-provider names
Cursor-based pagination via `before_id`, case-insensitive substring search across message + provider_id + JSON-stringified metadata. Per-provider names instead of bare IDs.

### v2.8.4 — activity log: full request/response payload capture
Embed serialized request + response bodies (up to 50KB each, scrubbed of secrets) into `event_meta` so the activity log captures the full call shape including tool calls.

### v2.8.3 — cluster sync respects `updated_at` for active providers
Race fix: cluster-sync was occasionally resurrecting soft-deleted providers.

### v2.8.2 — priority auto-bump + soft-delete + sync convergence
Insert/update with conflicting priority chains a deterministic auto-bump. Tombstone-aware soft-delete via `deleted_at` column.

### v2.8.1 — UI cleanup pass
Remove OAuth Capture page (legacy), refresh Routing docs.

### v2.8.0 — model-slug shortcuts + auto-routing + re-auth UI
OpenRouter-parity `:floor` / `:nitro` / `:exacto` suffixes; `model: "auto"` lets LMRH pick provider AND model; in-form re-auth flow for claude-oauth providers.

---

## v2.7.x — Claude Pro Max OAuth provider, hardening

### v2.7.8 — Tier 2 hardening sweep
Activity log indexes (`ix_activity_log_*`), API keys hot-lookup index, claude-oauth auth-failure 24h breaker, BUG-005 / BUG-010 / BUG-017 fixes.

### v2.7.7 — in-place claude-oauth re-auth from the edit form
Rotate tokens via `/oauth-rotate` endpoint while editing; no need to re-create the provider.

### v2.7.6 — Tier 1 + quick-wins remediation sweep
*(Last touch on README before v3.0.7's refresh.)*

### v2.7.5 — comprehensive live-test coverage + production fixes
End-to-end script (`scripts/test_claude_oauth_live.py`) exercising tool_use, streaming, vision, prompt caching against real Claude Pro Max accounts.

### v2.7.4 — scan-models support
List models via `platform.claude.com/v1/models`.

### v2.7.3 — Claude Code system marker + native test path
Anthropic returns masked `rate_limit_error` without the marker; mandatory.

### v2.7.2 — real Claude Code OAuth endpoint + CODE#STATE paste
Pulled real endpoints from the claude-code binary; replaces the initial guess.

### v2.7.1 — Claude Pro Max as a provider
Browser-initiated OAuth, PKCE, encrypted-at-rest tokens.

---

## Maintaining this file

When cutting a new tag:
1. `git tag -a vX.Y.Z HEAD -m "vX.Y.Z — short description"`
2. Add a section to this file in chronological-reverse order
3. Lead with the *why* and *what behavior changes* for operators / API consumers — not just *what files changed*
