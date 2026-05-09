# Changelog

All notable changes since v2.7.6. Older history available in `git log`.

The project follows [Semantic Versioning](https://semver.org/) loosely:
**major** = breaking API changes; **minor** = additive features; **patch** = fixes.

---

## v3.4.x — LMRHv2 Phase 3 (cost split + SSE push)

### v3.4.0 — Per-direction cost split + SSE stream + tighter probe latency

LMRHv2 Phase 3 lands as a single bundle plus a small probe-latency tightening informed by v3.3.3+ telemetry.

- **Per-direction cost split** — `app/monitoring/pricing.py` + `app/monitoring/metrics.py` + `app/monitoring/helpers.py` + `app/routing/lmrh/snapshot.py`. New `estimate_cost_split()` returns `(input_cost, output_cost)` tuple; `estimate_cost()` becomes a thin sum wrapper. `record_request` accepts a `cost_split` parameter and writes to four new `provider_metrics` columns (`input_cost_usd`, `output_cost_usd`, `input_tokens`, `output_tokens`). LMRHv2 snapshot now reports `cost_per_1m_input_usd` and `cost_per_1m_output_usd` as truly independent rates rather than the combined-rate placeholder. Schema migration is idempotent. Legacy callers that don't pass `cost_split` get a token-proportional heuristic so per-direction columns still populate.
- **`/lmrh/stream` Server-Sent Events endpoint** — `app/api/lmrh_v2.py`. Push semantics for clients that prefer subscribe-once vs polling every 30s. On connect emits `event: snapshot` with full payload + ETag as event id; subsequent ETag changes push fresh snapshots; configurable heartbeat (`heartbeat_sec` query, 10-120s, default 25) prevents proxy idle-timeouts. Per-key auth + per-key scope filter same as `/lmrh/providers`. `/.well-known/lmrh-config` advertises the new endpoint + `polling.stream_recommended: true`.
- **Subscription quota disclosure** — already wired since v3.3.0; verified end-to-end. Three providers (Devin-Anthropic-Max-Gmail, Devin-Anthropic-Max-VG, Devin-Codex-Gmail) report `subscription_quota` block correctly. Added v3.3.5 spec doc note.
- **Probe latency tightening 30s → 20s** — `app/config.py`. Default `grok_web_user_timeout_sec` lowered after a day of v3.3.3+ telemetry showed p95 ~7s with only 2 outliers >10s in 24h. 20s is still 3× headroom over real p95 while cutting tail-latency damage to user requests.

Schema:
- `ALTER TABLE provider_metrics ADD COLUMN input_cost_usd REAL DEFAULT 0`
- `ALTER TABLE provider_metrics ADD COLUMN output_cost_usd REAL DEFAULT 0`
- `ALTER TABLE provider_metrics ADD COLUMN input_tokens INTEGER DEFAULT 0`
- `ALTER TABLE provider_metrics ADD COLUMN output_tokens INTEGER DEFAULT 0`

Tests: +8 in `tests/unit/test_v340_phase3_and_split.py` (split function, total wrapper, unknown-model, record_request split write, fallback heuristic, well-known stream advertisement). 1014 → 1022 passing.

## v3.3.x — LMRHv2 bidirectional metrics feedback channel

### v3.3.5 — Conversation rotation + import cleanup

Two unrelated cleanups closing out the 2026-05-09 backlog.

- **Conversation rotation for grok-web** — `app/providers/grok_web.py`. New optional `extra_config.conversation_ids` (list) lets the operator pre-create 2+ grok.com conversation UUIDs and have the proxy round-robin across them per dispatch. Helps when grok.com applies per-conversation throttling. New helper `_pick_conversation_id()` round-robins across the pool with per-provider counter state; falls back to the single `conversation_id` for v3.2.x back-compat. Validator (`_validate_extra_config`) now accepts either field. Cloudflare still blocks programmatic `/conversations/new` from server IPs, so the operator must manually create the convs in a browser and paste UUIDs. Default behavior unchanged: providers without `conversation_ids` keep using their single `conversation_id`.
- **Import cleanup pass** — pyflakes flagged 87 unused imports across `app/`. Removed 21 in three high-confidence top-level files (`api/messages.py`, `api/completions.py`, `routing/router.py`) — clear-cut cases like unused `litellm`, `time`, dead `parse_hint` / `run_cot_pipeline` / `post_webhook` re-imports. Skipped `pipeline.py` aliases, `__init__.py` re-exports, and `auth/keys.py` rate-limit-state internals — those follow re-export patterns where pyflakes is unreliable.

Tests: +11 in `tests/unit/test_v335_grok_conv_rotation.py` (round-robin, fallback, validation, isolation across providers). 1003 → 1014 passing.

### v3.3.4 — Probe observability split + LMRHv2 probe channel

Two related cleanups completing the probe-vs-user-traffic story started in v3.3.3.

- **#3 Distinct `event_type='keepalive_probe'`** — `app/monitoring/helpers.py`. Synthetic probes now log under `event_type='keepalive_probe'` instead of overloading `'llm_request'` with a `[probe]` message prefix. Cleans up dashboard filters and SQL aggregates. Side-effect fix: 4 internal readers (cache-stats, billing-rollup, provider usage session/weekly windows) were inadvertently summing probe in/out tokens into user-facing totals — now they're user-only by construction. Memo `reference_llm_proxy2_db_query_gotchas.md` updated.
- **#4 LMRHv2 `probe_success_rate` + `probe_samples`** — `app/routing/lmrh/snapshot.py` + `app/api/lmrh_v2.py` + `sdk/python/lmrh_client.py`. After v3.3.3 hid probes from `success_rate`, this exposes them as a separate channel. Computed from `activity_log` rows with `event_type='keepalive_probe'` over the same window as `success_rate`. SDK `ModelMetrics` gets two new optional fields with `None` / `0` defaults so older proxies degrade gracefully. ETag input includes the new fields so probe-channel state changes still bust the cached snapshot.
- **#5 Spec doc** — `docs/lmrh-2.0-bidirectional.md` adds a "Probe vs user-traffic metrics" subsection under `/lmrh/providers` explaining the split, why it's a leading indicator (probe failures while user traffic succeeds = upstream throttling that hasn't tripped real traffic yet), and the SDK back-compat story.

Tests: +5 in `tests/unit/test_v334_probe_event_type_and_metrics.py`. 998 → 1003 passing.

### v3.3.3 — Grok-Web resilience pack

Four targeted fixes to the grok-web provider's reliability profile, all driven by the 2026-05-09 24h log audit. Pattern: 11 of 13 daily warnings on grok-web were synthetic-probe rate_limit (429) hits; success rate read 93.3% but the failures were probe-only — real user traffic was succeeding. Fix-set lifts apparent reliability to ~100% on user-facing metrics and reduces grok.com pressure during throttle windows.

- **#1 Probe back-off after 429** — `app/monitoring/keepalive.py`. When a keepalive probe hits a rate_limit error, double the next-probe delay (`interval_sec × factor^N`, capped at `keepalive_probe_rate_limit_backoff_max_sec`, default 1800s). Reset on first non-rate-limit outcome. Pre-fix, probes fired every 5 min unconditionally — when grok.com rate-limited us, the next probe 5 min later re-hit the same window. New module-level dicts `_probe_backoff_until` + `_consecutive_rate_limits`; new `get_backoff_state()` for diagnostics.
- **#2 Probes excluded from `provider_metrics`** — `app/monitoring/metrics.py` + `app/monitoring/helpers.py`. New `is_probe: bool = False` parameter on `record_request()`; when True, the function early-returns before touching the ProviderMetric upsert or ApiKey totals. Probe outcomes still hit `activity_log` (operator visibility) and `circuit_breaker` (state transitions). LMRHv2 callers reading `success_rate` now see user-traffic reality, not noisy synthetic-probe failures.
- **#3 Bridge fast-fail on recent 429** — `grok_bridge/app.py`. `_post_to_grok` records the timestamp of any 429 from grok.com; subsequent `/api/chat` calls within `GROK_429_COOLDOWN_SEC` (default 60s) short-circuit with a synthetic 429 + `Retry-After` header instead of round-tripping. Cuts grok.com pressure during throttle windows and lets the proxy router fall through to the next provider faster than waiting for a second refusal. New `rate_limit_429` block on `/api/status` exposes the cool-off state.
- **#4 Tighter outer timeout on user-traffic grok-web calls** — `app/api/_grok_web_dispatch.py`. New `_user_call_timeout()` reads `settings.grok_web_user_timeout_sec` (default 30s, down from 60s) and passes it to `complete_grok_web` / `stream_grok_web` / `stream_grok_web_anthropic`. Probes still use `_PROBE_TIMEOUT_SEC=15`. Caps p99 user latency to 30s instead of 60s on bridge tail-latency outliers (15.2s observed in the audit).

New settings:
- `KEEPALIVE_PROBE_RATE_LIMIT_BACKOFF_MAX_SEC` (default 1800)
- `KEEPALIVE_PROBE_RATE_LIMIT_BACKOFF_FACTOR` (default 2.0; ≤1.0 disables back-off)
- `GROK_WEB_USER_TIMEOUT_SEC` (default 30)
- `GROK_429_COOLDOWN_SEC` (default 60; bridge env var)

Tests: +10 (`tests/unit/test_v333_grok_web_resilience.py`). 988 → 998 passing.

### v3.3.2 — Public LMRHv2 spec doc + discovery polish

Three small additions to make LMRHv2 self-documenting for callers:

- **`docs/lmrh-2.0-bidirectional.md`** — public-facing spec. Mirrors the style of the `lmrh-1.2-*.md` family. Covers all five v2 endpoints, the discovery `Link` header, polling guidance, ETag round-trip, scope filter, per-key overrides, the SDK quick-start, and a Phase-3+ roadmap.
- **`/lmrh/v2.md` route** — the proxy now serves the public spec at this path (analogous to `/lmrh.md` serving the v1 draft). New readers don't need to find `/docs/` by hand.
- **Discovery improvements**: the `Link` header on `/v1/*` responses now carries a third entry `</lmrh/v2.md>; rel="lmrh-spec"`, and `/.well-known/lmrh-config` advertises `endpoints.spec` + `endpoints.providers_one` + `endpoints.quotes` for completeness.

No code-behavior changes; no tests added. README + CHANGELOG bumped to v3.3.2.

### v3.3.1 — `/lmrh/quotes` dry-run scoring + Python SDK reference

Phase 2 of the LMRHv2 protocol (operator decisions locked 2026-05-09).

- **`GET /lmrh/quotes?model=X[&hint=...]`** — pre-flight an inference request without dispatching. Returns the proxy's ranked candidate list (same scoring path as `/v1/messages`, just stops before winner-pick + dispatch) enriched with predicted cost / latency / TTFT / success_rate from the snapshot. Sophisticated callers see what WOULD happen for a given hint. Default rate limit 60/min vs providers' 4/min (per-call, less cache-friendly than bulk). Implementation: new `dry_run=True` mode on `select_provider()`.
- **`sdk/python/lmrh_client.py`** — single-file Python SDK. Background polling thread (60 s default, ETag-aware so steady-state polls return 304). Graceful 404 degradation. `build_hint(task=, prefer=, model_family=, region=, ...)` synthesizes RFC 8941-shaped headers from caller preferences. `prefer="most_reliable"` weights `success_rate × log(samples)` so 1.0 with 1 sample doesn't beat 0.99 with 600 samples.

Tests: +13 (5 endpoint + 8 SDK). Total 977 unit + 11 SDK = 988.

Live-verified: `/lmrh/quotes?model=x-ai/grok-4` ranks Grok-Web-Devin (#1, score 999, 18 samples) above OpenRouter (#2). SDK live-smoke against the proxy produces correct hints for all four `prefer` modes including resolved provider-id for `most_reliable`.

### v3.3.0 — LMRHv2 Phase 1 (bidirectional metrics feedback)

First major-feature surface of LMRHv2 (operator-approved 2026-05-09). Read-only metrics endpoints so LMRH-aware clients see live provider/model cost, latency, success-rate, and circuit state for their next request. **Default-off** via `lmrh_v2_enabled` feature flag — the flag is stored in cluster-synced `SystemSetting`, so flipping on one cluster node propagates to peers. Isolated nodes (`CLUSTER_ENABLED=false`, e.g. smoke) stay off until manually flipped.

New endpoints (under existing `/llm-proxy2/`):
- `GET /.well-known/lmrh-config` — server metadata, RFC 8615 well-known URI
- `GET /lmrh/providers` — live snapshot, key-scoped, ETag-cacheable, 30 s `max-age`
- `GET /lmrh/providers/{id}` — single-provider deep view; 404 hides operator-private providers
- `GET /lmrh/health` — aggregate fleet counters

Discovery: every `/v1/*` response carries `Link` header (RFC 8288) plus `LMRH-Version` (1.2 default-off, 2.0 enabled). Backward-compatible — v1.x clients unaffected.

Architecture:
- `app/routing/lmrh/snapshot.py` — in-memory snapshot, 30 s background refresh loop. Per-node, no cross-cluster sync (underlying ProviderMetric is already cluster-replicated).
- `app/api/lmrh_v2.py` — endpoint router. Per-key sliding-window rate limit (4/min providers, 60/min quotes), with `ApiKey.lmrh_polling_rpm` / `lmrh_quotes_rpm` overrides.
- ETag round-trip on `/lmrh/providers` so clients return `304 Not Modified` between snapshot refreshes.

Tests: +9 (snapshot + endpoints). 953 → 964. Operator decisions locked: see `project_lmrhv2_design.md` §8 in memory.

---

## v3.2.x — grok-web (cookie replay) + Playwright bridge sidecar

### v3.2.12 — `api_key_prefix` denormalized into activity_log event_meta

Self-contained log entries — no JOIN against `api_keys` needed. `record_outcome` now looks up `ApiKey.key_prefix` once per event and writes it to `event_meta.api_key_prefix` on both success and failure paths. The magic `key_record_id` "probe-keepalive" gets a literal `"probe-keepalive"` prefix so probe events stay filterable. Unknown / deleted-key references render as `None`. Bonus: sanitized one stale row in production where an earlier fix-it script had written a sha256 hash into the `key_prefix` column. +4 tests. 956 → 960.

### v3.2.11 — Playwright `/conversations/new` + auto-stamp event listener

Two improvements off the v3.2.10 backlog:

- **Bridge `/api/conversation/new`** drives Chromium UI to send a one-token "hi" message, harvests the resulting `/c/<uuid>` redirect. Uses Playwright Locator API (auto-retries on stale DOM that ElementHandle.click() stumbled on). In-browser `fetch()` to `/conversations/new` confirmed still 403'd by Cloudflare anti-bot even from real-browser TLS context — anti-bot is on the URL pattern, not just fingerprint. Live-verified: returned `e01d81f8-…` conversation_id; new UUID serves inference end-to-end. Wizard exposes a "Create new" button.
- **`app/models/_user_edit_stamp.py`** — SQLAlchemy `before_update` event listener auto-bumps `Provider.last_user_edit_at` when user-meaningful columns change. Background-rotation columns (api_key, oauth_refresh_token, oauth_expires_at, deleted_at, updated_at) excluded — those are exactly what the v3.0.11 stamp design was built to ignore. Belt-and-suspenders for the v3.2.7 cluster-sync fix: even direct DB writes now signal "this is a real edit". Explicit caller stamps (e.g. data import) still respected. +7 tests. 953 → 960.

### v3.2.10 — grok-web observability (record_outcome + keep-alive + cost-class)

Two real bugs surfaced when the operator asked "0 traffic to grok new provider... not even a search for grok in activity? keep alives working?":

1. **grok-web traffic was completely invisible** to ProviderMetric, activity_log, circuit_breaker, and per-key budget tracking. The v3.2.0 dispatch path bypassed `record_outcome` entirely. Pre-fix: `grok-web 24h: reqs=0 ok=0 fail=0` despite verified live calls.
2. **Keep-alive probes never ran for grok-web** — only OAuth subscriptions were probed. Bridge session staleness wouldn't surface until organic traffic 401'd.

Three fixes:
- `app/api/_grok_web_dispatch.py`: both helpers now call `record_outcome` on every terminal state. Streaming wrappers count chars for token estimates (4-char/token heuristic — grok.com web doesn't return per-chunk usage).
- `app/monitoring/keepalive.py`: `SUBSCRIPTION_TYPES` extended with `grok-web`; new `_probe_one` branch dispatches via `complete_grok_web`.
- `app/monitoring/helpers.py`: `SUBSCRIPTION_TIER_PROVIDER_TYPES` extended with `grok-web` so cost-class stays subscription.

+3 tests. Live-verify: `ProviderMetric reqs=2 ok=2 fail=0` after 1 organic + 1 probe in 10-min window.

### v3.2.7 — Cluster-sync LWW: tie-break fall-through + tz-naive normalization

**The bug:** v3.0.63's strict-greater check on `last_user_edit_at` correctly broke a ping-pong scenario, but had an unintended side effect: when both nodes carried the SAME `last_user_edit_at` and only one side's `updated_at` had moved (background mutation, direct DB write, sync-cascade flush), the receiving peer rejected the change entirely. Surfaced 2026-05-08 when an `extra_config.bridge_url` change on www01 didn't reach www02/smoke/GCP for hours — the peers had to be hand-fixed node-by-node.

**The fix:** when peer and local `last_user_edit_at` are EQUAL (real tie, not "missing stamp"), fall through to the legacy LWW path on `updated_at` with strict-greater. This catches background mutations without re-introducing the v3.0.63 ping-pong: genuinely-converged state (same user-edit + same updated_at) still rejects the inbound payload.

**Bonus:** `_parse_iso` now strips `tzinfo` and returns naive UTC. The legacy LWW path on line 187 was always going to TypeError in production whenever both `peer_updated_at` and `local_updated` were non-None, because SQLAlchemy returns naive datetimes from SQLite. The error was getting swallowed by the outer apply_sync handler — now the comparison just works.

Coverage: `tests/unit/test_cluster_sync_lww.py` adds 4 cases (strict-greater anti-ping-pong, tie + newer updated_at, peer newer user-edit accepts, peer older user-edit rejects-even-with-newer-updated_at). All pass; no regressions in the broader 900-test suite.

### v3.2.6 — Cross-node bridge access + UI polish

The `/grok-bridge/api/chat` location no longer goes through `auth_request` — peer llm-proxy2 instances (www02, smoke, GCP) call the bridge over the public URL `https://www.voipguru.org/grok-bridge/api/chat` with `X-Bridge-Token`, enforced inside the bridge container itself. Login/control-plane paths (/login, /vnc/, /api/status, /api/login/start) remain admin-session gated. Provider records cluster-sync the public URL.

UI polish:
- "Use bridge's current" button shrunk to "Use bridge's" (UUID in tooltip), only renders when the form's conv_id differs from the bridge's current page UUID.
- Default Model placeholder type-aware: 'grok-3' for grok-web, 'openai/gpt-4o' for openrouter, 'claude-sonnet-4-6' for OAuth.
- Mode-tab buttons gain focus-visible rings for keyboard accessibility.

### v3.2.5 — Bridge boots to grok.com; current_conversation_id surfaced

Two small wins on top of the v3.2.x stack:

- **Bridge lifespan navigates to `https://grok.com/`** on container boot instead of leaving Chromium on `about:blank`. The persistent `/data/playwright-state` volume already preserves the operator's session across restarts; this just makes the noVNC view show something useful immediately and gives Cloudflare a chance to passively refresh cookies.
- **`/api/status.current_conversation_id`** parses the bridge's current page URL — when Chromium is sitting on `grok.com/c/<UUID>`, the wizard surfaces a one-click **"Use bridge's current"** button next to the conversation_id field. Eliminates copy/paste from a noVNC screenshot.

### v3.2.4 — Wizard auto-populates bridge URL on mount (form blocker fix)

Symptom: operator selects grok-web in Add Provider, fills `conversation_id`, hits Create → backend rejects with "missing extra_config fields ['cookie_header','conversation_id']". The wizard's Bridge tab was visually selected by default but `bridge_url`/`bridge_token` only got injected into `extra_config` when the operator *clicked* the tab — and they didn't, because it was already selected. Fix: a `useEffect` runs on mount when `mode === 'bridge'` and prefills both fields from the wizard's defaults if absent.

### v3.2.3 — Backend validator allows bridge mode without cookie_header

The v3.2.0 grok-web validator hard-required `cookie_header` + `conversation_id` regardless of mode. Updated to two valid shapes: bridge (requires `bridge_url` + `conversation_id`, cookie_header optional) or manual (requires both as before). Error messages reworded to nudge operators toward Bridge mode first.

### v3.2.2 — Frontend wizard with Bridge / Manual tabs

`GrokWebProviderFields` component replaces the inline grok-web block in `ProviderForm`. Bridge tab is the recommended path: shows live bridge status (`✓ Signed in` once OAuth completes), 5-second status poll, "Connect Grok" button that opens the noVNC tab. Manual tab preserves the v3.2.0 cookie-paste flow as a fallback for operators who don't want to run the bridge container.

### v3.2.1 — Bridge mode wired into grok_web dispatcher

`extra_config.bridge_url` switches the dispatcher from local HTTP replay to forwarding the request body to the bridge's `/api/chat`. Bridge owns the cookies and handles 401/403 retries via Playwright `page.reload()` — Cloudflare challenges resolve passively because it's a real browser. Streaming in v3.2.x is buffer-then-emit (bridge collects the full NDJSON, dispatcher synthesizes SSE chunks); end-to-end token streaming through the bridge is a future enhancement.

### v3.2.0 — `grok-web` provider type (cookie replay)

Adds a new provider type that lets operators bring their grok.com web subscription (Lite / Premium) into the proxy without an xAI API key. We replay the browser's request shape against `https://grok.com/rest/app-chat/conversations/{id}/responses` using cookies + headers captured from a logged-in cURL.

**What works**: `/v1/chat/completions` and `/v1/messages` (both streaming + non-streaming), `grok-3` (modeId=fast), `grok-4` (modeId=expert).

**Single-conversation reuse**: `POST /conversations/new` is rejected by Cloudflare anti-bot from server IPs. Operator supplies one existing conversation_id; each proxy call sends `parentResponseId: ""` so callers don't share thread context inside that conversation.

**Auth model**: cookies (`cf_clearance`, `__cf_bm`, `sso`, `sso-rw`, `x-userid`) + headers (`x-statsig-id`, custom `user-agent`) live in `Provider.extra_config`. `cf_clearance` rotates every few hours — manual mode requires re-pasting periodically. v3.2.1+ bridge mode handles this passively.

**Bridge sidecar (v3.2.1+ companion service)**:

A separate container `llm-proxy2-grok-bridge` runs Playwright + Chromium + Xvfb + noVNC + a tiny FastAPI control plane:

- Persistent state volume `/data/playwright-state` survives restarts; operator signs in once via Google OAuth in the noVNC tab and the session is held indefinitely.
- 25-minute background refresh loop visits grok.com so Cloudflare passively reissues `__cf_bm`/`cf_clearance` before they expire.
- Exposed at `/grok-bridge/` via nginx; gated behind `auth_request /grok-bridge-auth-check` which validates the operator's `llmproxy_session` cookie against `/api/auth/me`. Anonymous hits get 302→`/llm-proxy2/?bridge_login_required=1`.
- `POST /api/chat` is the inference surface llm-proxy2's grok-web dispatcher calls (over the docker-compose internal network — never through nginx).

Build: `grok_bridge/` directory with `Dockerfile`, `app.py`, `start.sh`, `supervisord.conf`. Image `llm-proxy2-grok-bridge:latest` (~1.2 GB; based on `mcr.microsoft.com/playwright/python:v1.45.0-jammy`).

---

## v3.0.x — Run runtime, cluster ops, observability

### v3.1.2 — Bulk catalog cluster-sync (replaces per-row apply; default re-enabled)

`cluster_sync_catalog_tables` flipped back to default **True** after reworking the apply path that originally caused the 2026-05-07 60s `/v1/messages` hang incident.

**Old path (v3.0.96 → v3.0.98 hotfix disabled it)**: per-row `SELECT` then `INSERT/UPDATE` for every `ModelCapability` row in every sync push. With 304 rows × DB round-trip = 12-17s per sync, DB ~50% contended every minute, real `/v1/messages` calls queued past nginx's 60s upstream timeout.

**New path**: ONE bulk `SELECT` pulls every existing row whose `(provider_id, model_id)` matches any incoming row. Per-row LWW diff happens in memory. Inserts go through `db.add()`, updates mutate the loaded ORM instance — all flushed in a single batch on commit.

ON CONFLICT was tried first but rejected: the table's PK is an autoincrement `id`, not a composite on `(provider_id, model_id)`, so there's no UNIQUE constraint to conflict against. Adding one would need a migration with dup-detection — overkill for this win.

**Benchmark on 304-row dataset (www01)**:
- First sync after enable: ~2s (one-time apply of 304 rows where peer_updated > local)
- Steady-state apply: 48-52ms (LWW skips when peer_updated == local_updated)
- Live deploy showed sync p50=106-162ms, p95=109-169ms in real traffic

**Cross-node convergence**: confirmed within first sync cycle (~60s). www02 went from ~0 caps to 295 in one cycle; www01 has 304 because 9 of its rows are orphan caps for a deleted-and-purged provider (`e5e3905b79d1`) — www02's FK pre-filter correctly refused to materialize them. Working as designed.

**Behavior**: identity = `(provider_id, model_id)`, LWW by `updated_at` when both have a stamp. Same semantics as the per-row code; just batched.

### v3.1.1 — Test fixture hard-purge endpoint + pytest_sessionfinish hook

Closes the test-tombstone leak that caused the 2026-05-07 cycle-3 cleanup of 127 stale `pytest-*` / `test-playwright-*` / `debug-*` rows.

**New endpoint** (admin-only): `POST /api/keys/_purge-test-tombstones` hard-deletes tombstoned api_keys whose `name` matches a test pattern AND whose `deleted_at` is older than 60s (cluster-sync convergence buffer). Patterns: `pytest-%`, `pytest-cot-%`, `test-playwright-%`, `cot-debug-%`, `debug-%`. Admin-gated; safe to call in production.

**conftest.py**: new `pytest_sessionfinish` hook calls the endpoint after every test session, hard-purging any orphans the session left behind. Best-effort — failures don't fail the session.

**Playwright fix**: `test_create_api_key_flow` was the leak source — used hardcoded name `test-playwright-key` and never deleted it. Now uses unique `test-playwright-{uuid}` + `try/finally` cleanup that calls the standard DELETE endpoint. The session-finish hook is the safety net.

Without these, every soft-delete from a test run sat in the cluster_sync apply pass for the full 7-day tombstone retention window. Across many CI runs this slowed apply_sync the same way the 127-tombstone incident did.

### v3.1.0 — Architectural refactor: shared provider-selection + OAuth endpoint extraction

Two refactors shipped together. Both motivated by today's incident chain
(v3.0.99 capability-filter bug + coord-hub red-dots saga) revealing
two structural smells: silent divergence between the `/v1/messages` and
`/v1/chat/completions` provider-selection blocks, plus a 1136-line
`providers.py` with two near-identical OAuth flow trios.

**Refactor 1 — shared provider-selection**: Added `select_provider_with_503`
and `resolve_auto_model_into_body` to `app/api/_request_pipeline.py`. Both
endpoints now go through identical code for routing — closes the divergence
class that caused v3.0.99. `messages.py` and `completions.py` lose ~50 lines
of try/except + auto-routing each.

**Refactor 2 — OAuth endpoint extraction**: Moved 6 OAuth endpoints
(claude-oauth + codex-oauth × authorize/exchange/rotate) from `providers.py`
(1136 lines) to new `app/api/providers_oauth.py` (340 lines). Parameterized
via `OAuthProviderSpec` dataclass with two constants (`CLAUDE_OAUTH_SPEC`,
`CODEX_OAUTH_SPEC`). Three inner handlers (`_do_authorize`,
`_do_exchange_create`, `_do_rotate`) are shared. Adding a third OAuth
provider type (Vertex, Azure-AD, Bedrock) is now ~30 lines.

**Behavior**: zero changes. Same endpoints, same paths, same wire shapes.
904/904 unit tests pass. Live smoke verified all 6 wire-format/model
combinations (`/v1/messages` × claude/gemini/gpt, `/v1/chat/completions` ×
same). All 6 OAuth routes register correctly per `/openapi.json`.

**File-size impact**:
- `providers.py`: 1136 → 875 (-261)
- `providers_oauth.py`: NEW, 340
- `_request_pipeline.py`: 221 → 312 (+91)
- `messages.py`: 844 → 804 (-40)
- `completions.py`: 639 → 622 (-17)

Net file-line growth +113 (module header + docstrings + dataclass);
~300 lines of duplicated logic removed.

**Caught regression**: first deploy 500'd on `/v1/chat/completions` +
`gemini-2.5-flash` because `completions.py` had a stale
`requested_model` reference that I missed in the diff. Re-introduced
as a one-line local right after the new helper. ~10min from break to
fix; smoke probe caught it before fleet rollout.

See `refactor-log.md` for full details + extension-point documentation.

### v3.0.99 — `/v1/messages` capability filter (red-dots fix)

Coordinator-hub's UI showed every provider RED for days. Hub team's prober uses the Anthropic SDK against `/v1/messages` for ALL providers — so `gemini-2.5-flash` for Google providers, `gpt-4o` for OpenAI providers, `claude-*` for Anthropic providers. The non-claude probes 404'd with `not_found_error: model: gemini-2.5-flash` (or similar) and the hub marked the provider red.

**Root cause**: `/v1/messages` routing didn't filter providers by model capability. A `gemini-2.5-flash` request got force-routed to the highest-priority claude-oauth provider (Devin-Anthropic-Max-Gmail, prio=2) regardless of capability. We then forwarded the gemini model name to platform.claude.com, which doesn't have it → 404.

`/v1/chat/completions` had the capability filter wired up since v3.0.22 (it always passes the requested model name as `model_override`, which activates the v3.0.22 model-supports-by-provider filter + v3.0.36 family filter). `/v1/messages` had been Anthropic-shape-only for so long that nobody noticed it was passing `model_override=None` when no `ModelAlias` row existed.

**Fix**: 1-line change in `app/api/messages.py:172`. Pass `parsed_slug.bare_model` as `model_override` even without an alias. That activates:
- the family filter (`router.py:431`) which excludes claude-oauth from `gemini-*` / `gpt-*` / `cohere-*` requests
- the v3.0.22 model-supports-by-provider capability filter
- the v3.0.46 cross-family-fallback path when no provider matches the requested model exactly

Verified live on www01 (and confirmed in coord-hub's own activity log post-deploy):
- `POST /v1/messages` + `gemini-2.5-flash` → 200, served by Google Generative LLM (was 404)
- `POST /v1/messages` + `claude-haiku-4-5-20251001` → 200, claude-oauth path unchanged (control)
- `POST /v1/messages` + `gpt-4o` → 200, served by OpenAI provider with cross-family disclosure

904/904 unit tests pass. Hub flipped all provider dots GREEN on next probe cycle — first time in days.

### v3.0.98 — `/cluster/sync` 60s hang hotfix + codex probe token extraction + probe retention

URGENT INCIDENT FIX. Coordinator-hub team reported 60s hangs on `POST /v1/messages` with valid `llmp-CwLU` key — bad keys rejected fast (401 in 80ms, proving auth path was healthy) but valid keys hung exactly 60s with no first byte.

**Root cause**: v3.0.96 added `ModelCapability` + `ModelAlias` + `OAuthCaptureProfile` to the every-30s `/cluster/sync` payload. With ~304 ModelCapability rows × per-row `SELECT`-then-`INSERT/UPDATE` on the receiver, each sync POST grew from 200-700ms to **12-17 seconds**. With sync running every 30s and taking 13-17s, the DB was contended ~50% of every minute. Real `/v1/messages` calls queued waiting for DB pool slots and timed out at the 60s nginx upstream limit.

**Fix**: Catalog-table inclusion in `_build_sync_payload` is now gated by a new `cluster_sync_catalog_tables` setting, defaulting **OFF**. Restores v3.0.95-era sync payload + receiver workload. Sync latency post-fix: 200-919ms range. Operators who need cross-node `ModelCapability` sync can flip the setting; the proper rework (delta-only push + batched apply) is queued for a future release.

**Bundled (planned ship, kept atomic with hotfix)**:

- **codex keepalive token extraction**. Probe path now parses `response.completed` SSE event for `usage.input_tokens` / `output_tokens` instead of breaking out blindly. Pre-fix codex probe rows showed 0/0 every cycle.
- **probe-event retention**. New `activity_log_probe_retention_days` setting (default 7 days vs 30 for real events). Probes are 80%+ of fleet traffic when paperless is paused; 30 days of probe rows is wasteful.
- **`GET /api/monitoring/prune-status` endpoint** returns last sweep counts + retention config + activity_log row count. Lets operators verify the prune is firing without docker-exec.

### v3.0.97 — Close 3 logging blackouts + tombstone schema prep

Three call paths in admin / dispatch were returning to the caller without ever calling `record_outcome`, leaving the activity log silent for entire classes of traffic:

- **`dispatch_codex_oauth`** (both stream + non-stream paths). codex-oauth providers like `Devin-Codex-Gmail` had ZERO response-side log entries — the operator noticed when checking why probe rows showed reasonable latency but no usage info.
- **`POST /api/providers/{id}/scan`** — model-scan triggers from the admin UI weren't logged; operator-flagged "I don't see model scan requests in the logs."
- **`POST /api/providers/{id}/test`** — same pattern; admin test-provider clicks were invisible.

All three now `log_event` with metadata (operation, status summary, key counts).

Bundled schema-only prep for v3.0.98: added nullable `deleted_at DATETIME` columns to `model_capabilities`, `model_aliases`, and `oauth_capture_profiles`. Idempotent ALTER TABLE migrations. Sync logic deferred — turned out to be moot when v3.0.96's catalog sync caused the 60s hang and v3.0.98 disabled it by default.

### v3.0.96 — Replicate ModelCapability + ModelAlias + OAuthCaptureProfile (REVERTED IN v3.0.98)

Operator question after the v3.0.95 `/v1/models` fix: "what else may not be cluster-synced that needs to be?" Audit found 3 catalog tables missing from the every-30s sync payload, with predictable cross-node drift on www01 vs www02 (304 ModelCapability rows on www01, 0 on the others).

**Shipped** the additions to `_build_sync_payload` + matching apply-side blocks in `sync.apply_sync` (per-row SELECT-then-INSERT/UPDATE).

**Regression discovered same day**. With ~304 ModelCapability rows × per-row apply on the receiver side, each `/cluster/sync` POST grew from 200-700ms to **12-17 seconds**. Combined with the 30s push interval, the DB was contended ~50% of every minute, queueing real `/v1/messages` calls past the 60s nginx upstream limit. Coordinator-hub team caught it 6 hours after ship.

**v3.0.98 disabled this by default** behind `cluster_sync_catalog_tables` setting. Proper rework (delta-only push + batched `INSERT...ON CONFLICT`) deferred.

### v3.0.95 — `/v1/models` returns only `Provider.default_model`

Cross-node divergence: `GET /v1/models` returned 196 entries on www01 vs 5 on www02. Root cause: ModelCapability table wasn't cluster-synced (one-time discoveries on www01 leaked into the public-list response).

Fix: response now derives strictly from `Provider.default_model` of enabled, non-tombstoned providers — exactly the set that's already cluster-synced. No more hidden cap-table dependency. Same 5 entries everywhere.

### v3.0.94 — Activity log: split previews from full bodies; restore msg in/out

Operator post-v3.0.91 incident: "I see metadata in the activity logs but we had message in and response; where is that now?"

Root cause: v3.0.91 flipped `activity_log_capture_bodies` to default-False to stop the 1 GB activity_log incident, but that was a sledgehammer — operators still want to glance at *what* was sent without the full 50KB body capture cost.

Fix: split into two settings.
- `activity_log_capture_previews` (default **True**) — captures first 240 chars of request + 240 chars of response. ~500 bytes/row, bounded.
- `activity_log_capture_bodies` (default **False**) — full bodies up to `activity_log_max_body_chars`. Wire-debugging only.

**Operator-locked rule** (memory `feedback_keep_msg_in_out_logging.md`): previews stay default-True permanently. Operator-typed permission required to flip.

### v3.0.93 — Activity log rows always expandable

Regression from v3.0.91's body-capture flip: the click-to-expand UI hid most rows because `expandable = Boolean(reqBody || respBody || errorMsg)` returned false on the now-empty body fields. Fix: `expandable` now true when ANY metadata is present (route, hint, cache fields, error class, etc.) — rows always click-to-expand.

### v3.0.92 — Bigger DB pool + 30-min recycle (post-incident hardening)

Login 500s and `/v1/messages` queueing 17h after the v3.0.91 restart. Even with body capture disabled, the 1 GB residual rows were still slow on `json_extract` scans, and usage_tracker queries hammered the DB. Bumped `pool_size=50`, `max_overflow=100`, `pool_timeout=10s`, `pool_recycle=1800s`. Plus a one-off prune of 67,548 bloated rows (964 MB freed) on www01.

### v3.0.91 — Default `activity_log_capture_bodies` to False

URGENT INCIDENT FIX. Operator: "I get internal server error logging in." Root cause: 1 GB activity_log table (67k rows, average ~15 KB each), with bodies stored at 50000-char cap. Background `usage_tracker` queries did `json_extract` scans across the bloated rows and exhausted the DB pool. Login (which hit the same pool) returned 500.

Fix: `activity_log_capture_bodies` default flipped True → False. `activity_log_max_body_chars` cap dropped 50000 → 4000. Existing 1 GB pruned via the v3.0.92 sweep. Future operators who actually need wire-level body capture set the flag explicitly.

### v3.0.88-v3.0.90 — Error-class taxonomy refinements

Three follow-ups to v3.0.75's classifier so the histogram on the Metrics page stops bucketing real failures as `unknown`:

- **v3.0.88** — httpx exception names (`ReadError`, `WriteError`, `ConnectError`, etc.) classify as `network` instead of `unknown`. Surfaced when the proxy team's Anthropic backbone had a 30-min flap and operator couldn't tell from the dashboard whether the failures were upstream-network or proxy-side.
- **v3.0.89** — litellm SDK exception names (`BadRequestError`, `ContextWindowExceededError`, `AuthenticationError`) classify as `bad_request` / `auth` instead of `unknown`.
- **v3.0.90** — Anthropic-shape `529 Overloaded` body classifies as `upstream_5xx` (was `unknown`). The 529 isn't a 5xx code but Anthropic semantics treat it as transient-server, so we count it on the same pile.

### v3.0.87 — Shared cache-disclosure helper + `cache=ignored` override

Refactor of the inline LMRH 1.2 §E2 disclosure blocks shipped in v3.0.83-85: extracted to `app/api/_cache_inject.py:build_cache_disclosure` + `append_cache_disclosure`. Same logic, single source of truth.

Adds the spec §E2 substitution-interaction rule: when a caller sends `cache=<non-none>` but the served provider is non-Anthropic-shape (cross-family substitution), the dim cannot be honored → emit `cache=ignored` so the caller can audit the no-op. Previously these substituted calls just dropped the cache dim from the response header silently.

### v3.0.86 — Roll up Phase 2 status in cache-mode dim doc

Documentation only. Updated `docs/lmrh-1.2-cache-mode-dim.md` with the v3.0.83-85 disclosure status table (which dim values fire on `/v1/messages` vs `/v1/chat/completions`, streaming vs non-streaming).

### v3.0.85 — `cache-tokens-read` / `cache-tokens-written` disclosure on response headers

Phase 2 partial: `LLM-Capability` response now carries `cache-tokens-read=N, cache-tokens-written=N` (extracted from upstream usage block: `cache_read_input_tokens` / `cache_creation_input_tokens`). Non-streaming claude-oauth path. Lets callers audit *how much* their cache injection actually saved without parsing the response body.

### v3.0.84 — §E2 cache disclosure on `/v1/chat/completions`

Same disclosure shape as v3.0.83 but on the OpenAI-shape endpoint. Especially valuable here because the OpenAI response body strips the cache fields entirely — without the header echo, callers using a chat-completions backend would have no way to see the cache tokens.

### v3.0.83 — §E2 cache disclosure on `LLM-Capability` (Phase 2 partial, non-streaming claude-oauth)

LMRH 1.2 §E2 spec: when a request carries `cache=` dim, the response `LLM-Capability` must echo `cache=<mode>` and `cache-injected=?1` if the proxy auto-injected. Shipped on the non-streaming claude-oauth path first (the highest-volume path — paperless's stable legal-review template).

Streaming-path disclosure deferred: HTTP trailers aren't supported in FastAPI/Starlette, so disclosing on stream needs a synthetic SSE event before `[DONE]` — a separate spec discussion.

### v3.0.82 — `utc_iso()` applied to 4 stragglers

Audit found 4 sites still emitting timezone-naive timestamps (`datetime.utcnow().isoformat()`) — provider-usage endpoint + `audit_export.list_exports`. Routed through the central `utc_iso()` helper for consistent `Z`-suffixed UTC. Closed a pre-existing `audit_export` test failure.

### v3.0.81 — Hit-rate sparkline on Cache Savings card

Compact Recharts `LineChart` showing the last N hourly buckets of cache hit-rate %. Helps spot trend shifts (e.g. paperless template change cratered hit-rate from 93% → 50%).

### v3.0.80 — Time-series bucketing on `/api/monitoring/cache-stats`

Added `bucket_minutes` query param. Returns time-series array of hit-rate / read-tokens / written-tokens per bucket. Powers the v3.0.81 sparkline.

### v3.0.79 — Frontend `error_class` filter dropdown

Activity page gets a dropdown alongside the existing per-key + per-provider filters: filter by `auth` / `billing` / `rate_limit` / `timeout` / `network` / `upstream_5xx` / `bad_request` / `unknown`. Pulls the set dynamically from observed values.

### v3.0.78 — `error_class` filter on activity endpoints

Backend support for the v3.0.79 frontend filter: `error_class=` query param on `/api/monitoring/activity` and `/api/monitoring/activity/count`. Server-side filter, not post-fetch — keeps page-2+ working under high traffic.

### v3.0.77 — CSV download button on Cache Savings card

Frontend button that hits the v3.0.76 endpoint with the user's current filter selection. One click → billing-grade rollup CSV.

### v3.0.76 — `/api/monitoring/usage-report.csv`

Per-key / per-provider rollup of total tokens, cache tokens read/written, estimated cost, request count. CSV output for billing reconciliation. Ad-hoc operator tool that became permanent.

### v3.0.75 — Error-class taxonomy in `event_meta`

`record_outcome` now classifies error responses into a fixed bucket: `auth` / `billing` / `rate_limit` / `timeout` / `network` / `upstream_5xx` / `bad_request` / `unknown`. Stored on `event_meta.error_class`. Powers the v3.0.78-79 filter and the v3.0.88-90 refinements. Without this, the only signal in activity_log was the 500-char error_str blob — useless for at-a-glance triage.

### v3.0.74 — Provider/API-key toggle on Cache Savings card

Card defaults to per-provider grouping; toggle flips to per-api-key. Same data, different cut. Makes it easy to spot which caller is driving most of the cache-hit savings.

### v3.0.73 — Cache Savings card on Metrics page + `utc_iso()` bugfix

Frontend Recharts card showing 24h cache hit-rate, total tokens read from cache, tokens written, estimated $ saved. Feeds off the v3.0.72 endpoint.

Bonus fix: the `utc_iso()` bug that was causing one pre-existing `audit_export` test to fail (timezone-naive stamps in test fixtures) — patched the same release since the test was blocking the audit_export merge.

### v3.0.72 — `/api/monitoring/cache-stats` endpoint

Returns aggregate cache hit-rate, read-token total, write-token total, estimated $-saved (cache-read-tokens × per-model cache-discount price). Groupable by provider OR api_key via query param. Powers the v3.0.73 UI card.

### v3.0.71 — Echo `cache_read_input_tokens` / `cache_creation_input_tokens` to `event_meta`

`record_outcome` now extracts both fields from the upstream usage block and stores them on `event_meta.cache_read_input_tokens` / `event_meta.cache_creation_input_tokens`. Powers v3.0.72-74 dashboards. Without this, the cache savings audit had to grep response bodies — slow and unreliable when bodies aren't captured.

### v3.0.70 — `fallback-chain` alias + provider-family fuzzy match

Two LMRH-parser additions:

- `fallback-chain=...` is now a recognized alias of `provider-hint=...` (caller convenience — "fallback chain" reads more naturally than "provider hint" for an explicit ranked list).
- Provider-family fuzzy match: `provider-hint=anthropic` matches all 4 anthropic-shape provider types (`anthropic`, `claude-oauth`, `anthropic-direct`, `anthropic-vertex`) instead of requiring an exact `provider_type` match. Makes the dim usable for cross-vendor routing without callers needing to enumerate every implementation.

### v3.0.69 — `cache=ephemeral|none|off|disabled` mode dim (LMRH 1.2 Phase 1)

First wire-up of the LMRH 1.2 cache dim:
- `cache` registered as a builtin LMRH dim (no proposal needed)
- `cache=ephemeral` force-injects `cache_control` even below the auto-threshold (caller knows the prefix is stable; respects their judgment over the heuristic)
- `cache=none|off|disabled` opts out of auto-cache entirely (compliance / debugging / cost-attribution use cases)
- Default `cache=auto` = pre-v3.0.69 opportunistic behavior (no change for callers who don't send the dim)

Spec doc: `docs/lmrh-1.2-cache-mode-dim.md`. Phase 2 (response disclosure) shipped in v3.0.83-85.

### v3.0.68 — LMRH legacy parser: preserve comma-list values

Bug: `provider-hint=Devin-Anthropic-Max-VG,Devin-Anthropic-Max-Gmail;require` got truncated to just `Devin-Anthropic-Max-VG` because the legacy parser split on commas at the top level instead of respecting the dim's multi-value semantics. Composite hints with multi-value dims now parse correctly.

### v3.0.67 — Semantic cache + shadow embeddings honor provider pin

Bug: when a request had `provider-hint=X;require` and X was a non-embedding provider, the semantic-cache path's embedding lookup ignored the pin and used the default embeddings provider. Same with shadow-embeddings. Fix: respect the pin or skip the cache check (returns `bypass`). Prevents silent provider mixing under hard pins.

### v3.0.66 — Microsoft Azure OpenAI provider type

New `provider_type=azure-openai`. litellm-routed, OpenAI-shape requests/responses, but with Azure's two-stage URL pattern: `base_url + /openai/deployments/{deployment_name}/chat/completions?api-version=...`. Requires the deployment name in `extra_config.deployment` and api-version in `extra_config.api_version`. Capability inference reuses the OpenAI scanner.

### v3.0.65 — Auto-rotate provider priority on usage gap (Phase 3)

When a top-priority provider hits its weekly token cap on a subscription tier, automatically deprioritize it (priority moves toward the back of the list) until the new week starts. Stops the noisy "provider X cap reached" rate-limit cascade. Operator-overrideable via the v3.0.64 Usage UI.

### v3.0.64 — Usage tracking config UI + list-row indicator (Phase 2)

Per-provider weekly-cap config field on the Provider edit form. Provider list shows a colored indicator (green / yellow / red) for usage % toward weekly cap. No data fields shipped here — just the visualization for the v3.0.62 numbers.

### v3.0.62 — Per-provider session+weekly token tracking (Phase 1)

DB schema: `provider_token_usage` table tracking session (today) + weekly window. `record_outcome` writes here on every claude-oauth / codex-oauth / anthropic-oauth call. Powers the v3.0.64 UI + v3.0.65 auto-rotation. Subscription tier providers are the primary use case (free tokens up to N per week, then per-call billing kicks in — operators want hard visibility into where they sit on the cap).

### v3.0.63 — Strict-greater LWW on provider sync stops priority ping-pong

Bug: after v3.0.11 added `last_user_edit_at` for LWW gating, two nodes editing the same Provider row in the same second could ping-pong (each thought theirs was newer because comparison was `>=`). Fix: strict greater-than. Equal timestamps preserve the receiver's local copy; the next real user edit wins on the next sync. No more 4-second priority-flap incidents.

### v3.0.61 — Bigger DB pool + skip middleware on `/health`

Resilience hardening discovered during the 2026-05-05 internet-out incident: even with one upstream provider holding 300s timeouts, the LMRH-warning middleware was running the registry-cache refresh on every request (including `/health` and `/version`), which queued behind the DB pool exhaustion. Fixed two ways:
- `_LMRH_MIDDLEWARE_SKIP_PATHS` now includes `/health`, `/version`, `/metrics`, `/favicon.ico`. Liveness / observability paths remain answerable even if the registry cache is stalled.
- DB pool tuning ([was] pool_size=20 max_overflow=30 → pool_size=20 max_overflow=30, plus pool_timeout adjustments). Further tuning in v3.0.92.

### v3.0.60 — Split `httpx.Timeout` into connect/read/write/pool

Single `timeout=300` was wrong — a DNS / TCP-connect failure held the request for 300s while the upstream was confirmed dead. During the 2026-05-05 internet outage this exhausted the SQLAlchemy DB pool within seconds and locked up the whole proxy until container restart.

Fix: `httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)` on every outbound httpx call. Connect-phase failures now return in ~5s, freeing the DB connection back to the pool. Streaming reads stay at 300s for slow upstreams.

### v3.0.59 — Plumb `llm_hint` into non-OAuth Anthropic helpers

Companion to v3.0.58 — the same `llm_hint` plumbing for the litellm Anthropic path (`_stream_anthropic`, `_complete_anthropic`). Without this, `anthropic-direct` provider calls also reported `had_lmrh_hint=true` but no `lmrh_hint_raw`.

### v3.0.58 — Plumb `llm_hint` into claude-oauth dispatch

`event_meta.lmrh_hint` (added in v3.0.55) was capturing the header on FastAPI parse, but the claude-oauth dispatch path (`_stream_claude_oauth` / `_complete_claude_oauth`) was constructing its own `record_outcome` calls without the hint, so the activity log showed `had_lmrh_hint=true` but no `lmrh_hint_raw` for the highest-volume path. Threaded `llm_hint` through.

### v3.0.57 — Explicit per-provider cost_class column

Replaces the hardcoded `SUBSCRIPTION_TIER_PROVIDER_TYPES` set (introduced in v3.0.50) with a DB-backed `Provider.cost_class TEXT` column. NULL preserves the v3.0.50 default behavior (derive from `provider_type`: `claude-oauth`/`codex-oauth`/`anthropic-oauth` = `subscription`, all else = `per_call`). Admin-overridable when an `anthropic-direct` provider is on a flat-rate enterprise contract, or for any future per-call OAuth tier.

Idempotent ALTER TABLE migration. `record_outcome` prefers the explicit column when set; otherwise falls back to the type-based derivation, so existing deployments keep working unchanged.

### v3.0.56 — Skip keepalive probes on per-call providers

Cost-burn audit on 2026-05-04 found probes burning ~$0.32/day on synthetic Cohere traffic, plus smaller amounts on Vertex/Google/Personal-OpenAI. Annualized: ~$120/year on Cohere alone, and growing.

Subscription-tier providers (`claude-oauth`/`codex-oauth`/`anthropic-oauth`) keep probing — $0 per probe. Per-call providers are skipped by default. The auth-failure / billing-failure UI badge already surfaces dead state from real traffic.

Override: `KEEPALIVE_PROBE_PER_CALL_PROVIDERS=true` (env) restores pre-v3.0.56 probe-everything behavior.

### v3.0.55 — Cost-tier resolves against requested model + capture LLM-Hint header

Two fixes from a 2026-05-04 cost burn diagnosis ($1.59 of real billing in one day on what should have been a $0 subscription path).

**Root cause** — `capability_inference` derives `cost_tier` from the provider's `default_model`. Devin-Anthropic-Max-VG's default is `claude-sonnet-4-6` → tier `standard`. When a caller requests `claude-haiku-4-5` (which IS economy tier) with `cost=economy;require`, the hard filter excludes the claude-oauth provider despite the requested model being economy. Cross-family fallback fires → Vertex Gemini Flash → real per-call billing.

**Fix 1** — In `select_provider`, when caller specifies `model_override`, re-derive `cost_tier` from THAT model name (`haiku`/`flash`/`mini`/`gpt-3.5` → economy; `sonnet`/`gpt-4o`/`gemini-2.0` → standard; `opus`/`o1`/`o3`/`r1` → premium) and apply to family-aligned candidates before LMRH scoring. Family-type gating keeps the rewrite scoped — a Vertex provider doesn't get its tier rewritten just because the caller asked for "haiku".

**Fix 2** — `event_meta.lmrh_hint` (capped 500 chars) now captures the raw `LLM-Hint` header on every `llm_request` event. The 2026-05-04 diagnosis hit a wall because we couldn't see what hint the caller actually sent — only that they sent something (`had_lmrh_hint=true`). PII-free since LMRH dims are routing metadata, not content.

### v3.0.54 — claude-oauth marker doesn't add cache_control when caller has it

AI Analyzer reported v3.9.22 smoke test where two back-to-back identical-system-prompt calls (input=2059 tokens, claude-haiku-4-5, claude-oauth) returned `cache_creation=cache_read=0` despite the caller correctly attaching `cache_control: {type: "ephemeral"}` to the system block.

Root cause: claude-oauth path's `_inject_claude_code_system` was unconditionally adding `cache_control` to the prepended ~14-token Claude Code marker block. With a caller-supplied cache_control downstream, this created two breakpoints — breakpoint 1 (marker, ~14 tokens) below every model's per-request minimum (Sonnet 1024 / Haiku 2048 / Opus 4096). Sub-threshold breakpoints normally silently no-op, but in some upstream behaviors they suppress caching for the whole request.

Fix: when ANY caller-supplied system block carries cache_control, the marker block is emitted **without** cache_control. Single-breakpoint mode — caller's larger block is the only breakpoint. Marker text still anchors prefix start (cache key stability preserved). Original v2.7.6 case (caller didn't supply any cache_control) still wraps the marker.

**Followup finding (8h sample post-deploy)**: Sonnet caches at 97.1% hit rate on Devin-Anthropic-Max-VG; Haiku at 0.0% even at 2410 tokens (above documented 2048 threshold). `cache_control` on `claude-haiku-4-5` over Pro Max OAuth tier appears unsupported despite the prompt-caching beta flag being accepted — upstream-side limitation, not a proxy bug.

### v3.0.53 — Billing-error breaker hold-down 1h → 6h

Billing errors (quota exhausted, payment_required, insufficient_credit) need operator intervention — they don't self-resolve in an hour. The 1h hold-down meant each node fired a re-test probe ~24×/day per provider, contributing 1-3/hr cluster-wide log noise on quota-exhausted providers.

6h hold-down: 4 retests/day per node, still detects same-day recovery, ~75% less log churn while operator triages billing.

### v3.0.52 — LMRH 1.2 §E3 ;sovereign modifier + region disclosure headers

Completes the LMRH 1.2 §E3 region-pinning reference implementation:

- `HintDimension.sovereign: bool` field; `;sovereign` modifier parsed in both legacy and RFC 8941 paths (implies `;require`)
- Sovereign rejects providers with empty `regions` config (uncertainty = reject; differs from `;require` which soft-passes unconfigured profiles for backwards compat)
- `LLM-Capability` emits `served-region=<most-specific>` and `region-honored=strict|loose` whenever the caller sent a `region=` hint and a candidate matched
- 6 new unit tests (24/24 LMRH suite total)

`cross-border-risk` disclosure remains spec-only — needs per-provider-type failover-behavior metadata.

### v3.0.51 — LMRH region hierarchy + InnerList any-of matching

Extends the existing region-dim scoring (which already enforced `;require` as a hard filter) with hierarchy matching and InnerList any-of values:

- `region=eu` is now satisfied by a profile tagged `eu-west` / `eu-central` (and likewise `us` / `asia` / etc.)
- RFC 8941 InnerList syntax `region=(us ca)` (any-of) honored by scorer
- 6 new unit tests covering exact match, hierarchy, `;require` pass via hierarchy, `;require` fail, unconfigured-profile soft-pass, `any` token

### v3.0.50 — Subscription-tier zero-cost accounting

Closes A7 cost-attribution overcount on cross-family-substituted calls. When v3.0.46's cross-family fallback substitutes a request like paperless's `gpt-4o` to codex-oauth (operator's flat-rate ChatGPT Plus subscription), `record_outcome()` was still calling `estimate_cost()` with the substituted model + tokens and writing the litellm-rate value to `event_meta.cost_usd` and `api_keys.total_cost_usd`. Paperless's rolling cost ticker was reading ~$3-5/hr inflated.

Fix: classify provider_types as subscription-tier vs per-call. For subscription tier (`codex-oauth`, `claude-oauth`, `anthropic-oauth`), record `cost_usd=0.0` and surface the litellm-rate value as `quota_usd` for "what would this have cost on per-call billing" reporting.

Adds `event_meta.cost_class = "subscription"|"per_call"` on every `llm_request` event for consistent dashboard filtering. Mirrored on the error path. Provider lookup is one `db.get(Provider, id)` per record — primary-key indexed.

### v3.0.29 — LMRH dim/proposal tombstone replication + warning-cache invalidation

Hard-DELETE on a registered LMRH dim was reversed by the next cluster-sync push from a peer that still had the row — the receive-side merge was strict "insert if missing." Same class of bug fixed for `Provider` (v2.8.2) and `ApiKey` (v3.0.20).

- New `deleted_at REAL` column on `lmrh_dims` and `lmrh_proposals` (idempotent ALTER).
- New admin-only `DELETE /lmrh/registry/{name}` and `DELETE /lmrh/proposals/{id}` endpoints — soft-delete via `deleted_at = time.time()`.
- Cluster sync push payload now carries `deleted_at`. Receive-side merge: peer's tombstone propagates if newer than local; local tombstone preserved if peer has none. Insert-if-missing path skips materializing a peer's tombstone.
- Read endpoints + `known_dim_names()` (used by the warning middleware) all filter tombstoned rows.
- Re-registering a soft-deleted name resurrects the row in place to preserve `registered_at`.
- Bonus: `invalidate_registry_cache()` callback wired into register + delete handlers so newly-registered/-deleted dims are recognized immediately instead of after the 60s TTL window.

### v3.0.28 — Dark-mode invisible-text fix on activity + providers search inputs

Operator-reported bug: "can't type into the activity log search box." Root cause: the raw `<input>` elements on `ActivityPage` and `ProvidersPage` had `dark:bg-gray-900`/`dark:bg-gray-800` for background but no explicit text color. Browser default `color: inherit` resolved to a dark color from a parent container — dark text on dark background. Typing worked but the value was invisible. Fix: add `text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500`.

### v3.0.27 — Embedding-on-chat rejection

AI Analyzer team report: Cohere upstream returned 400 "model 'embed-english-v3.0' is not supported by the chat API." Root cause: a chat call with no `model` field selected the Cohere provider, and `build_litellm_model` fell back to `provider.default_model` which is `embed-english-v3.0` (the recommended embeddings default). Two-layer fix:

1. Reject `embed-*` / `text-embedding-*` / `embedding-*` model names at `/v1/chat/completions` and `/v1/messages` entry → HTTP 400 with pointer to `POST /v1/embeddings`.
2. In `select_provider`, when `model_override` is None, drop providers whose `default_model` is an embedding-only slug.

### v3.0.26 — Claude routing hard-fix + LLM-Hint header-name fix

DevinGPT verification of v3.0.25 surfaced two ship-blocking bugs:

- **Routing fall-through:** v3.0.22's capability filter had a fall-through that let codex-oauth eat `claude-*` requests on `/v1/chat/completions` (which excludes `claude-oauth` providers). Fixed with a hard model-family vs provider-type filter that runs BEFORE the capability filter:
  - `claude-*` → `{anthropic, anthropic-direct, anthropic-oauth, claude-oauth}`
  - `gpt-*`, `o1-*`, `o3-*` → `{openai, codex-oauth}`
  - `codex-*` → `{codex-oauth}`
  - `gemini-*` → `{google, vertex, vertex_ai}`
  - `embed-*`, `command-*` → `{cohere}`

  Empty list raises `RuntimeError` → propagates as 503 with an actionable message.

- **`X-LMRH-Warnings` middleware silently missing:** read `x-llm-hint`, but the canonical LMRH request header is `LLM-Hint` (no X- prefix). Fixed.

### v3.0.25 — LMRH self-extension protocol (registry, handshake, exclude=, warnings)

LMRH 1.1 — runtime extension protocol so callers can adopt new dim names without a proxy code change.

- `POST /lmrh/register` — auth-required, collision-resolved registration. Idempotent.
- `POST /lmrh/propose` — auth-required, free-form proposal queue for operator review.
- `GET /lmrh/registry` and `GET /lmrh/registry/{name}` — public discovery.
- New built-in dims: `exclude=PROVIDER` and `provider-hint=PROVIDER` (both case-insensitive name OR provider-type match; `;require` = hard).
- New header `X-LMRH-Warnings: unknown-dim:NAME register-at:/lmrh/register spec:/lmrh.md` on responses where the request carried unrecognized dims.
- Cluster sync replicates the dim registry + proposals queue across peers.
- LMRH RFC draft bumped to 1.1 with the extension-via-registration section.

### v3.0.24 — log-mining batch: normalize-ties scope + /health noise + litellm verbosity

Three improvements found in a 3h log review (no errors — just abnormalities worth fixing):

1. **`normalize_priority_ties` now scopes to active providers only** (`deleted_at IS NULL AND enabled=True`). Tombstoned + disabled rows no longer participate in tie detection — they don't route, so ties between them or with active rows don't matter for selection. Diagnostic clue: www01 was firing `cluster_sync_normalized_ties count=2` 45 times in 3h while www02/GCP fired 0; old logic was tripping on tombstoned-vs-active priority collisions during sync apply. Also enriched the log line to record which provider IDs got bumped (`details=[{id, from, to}, ...]`).

2. **`/health` endpoint cached for 3 seconds + silenced from access logs.** Docker healthcheck (every 30s) + cluster peer heartbeat (every 30s, 2 peers) hit /health ~270 times/hour per node. Each call ran a `SELECT * FROM providers WHERE enabled=True` + per-provider `is_available()`. Cache the body for 3s (well under heartbeat cadence); circuit-breaker state still computed live. Access log filter drops `/health` from `uvicorn.access` so real signals aren't buried.

3. **`litellm.set_verbose = False` + `LiteLLM` logger at WARNING** in lifespan. Was emitting per-call `LiteLLM completion() model=…` INFO lines (~109 per 3h on www01). Errors and warnings still surface; routine call chatter doesn't.

### v3.0.23 — embeddings + cohere + model kind tag + LMRH doc + codex reasoning_effort

Batch from the DevinGPT integration Q&A. Four asks shipped together:

- **`POST /v1/embeddings`** — OpenAI-compatible embeddings dispatch. Routes via the same `select_provider` machinery as chat (so the v3.0.22 model-supports filter automatically picks the right provider per requested model). Subscription OAuth providers (`claude-oauth`, `codex-oauth`) are excluded — neither exposes embeddings. Litellm-mediated, so any vendor litellm supports works (OpenAI, Cohere, Google text-embedding, Azure, Voyage, etc.).
- **`cohere` provider type** — primarily an embeddings home (also rerank/chat). Default model `embed-english-v3.0`. Scan endpoint hits Cohere's `/v1/models`. Add via the standard "Add Provider" form; paste your Cohere API key.
- **`/v1/models` now includes a `kind` field** per entry: one of `chat`/`embedding`/`image`/`audio`. Inferred from model name patterns. Lets clients filter their dropdowns to the right surface (e.g. `text-embedding-3-small` → embedding; `whisper-1` → audio). 176 models tagged, breakdown: 157 chat / 8 audio / 8 image / 3 embedding (will expand as cohere/voyage/etc. providers are added).
- **`/lmrh.md` (and `/lmrh`)** — public, no-auth route serving the LMRH RFC draft (`docs/draft-blagbrough-lmrh-00.md`). For cross-app integration docs to link without secret-handling. The `docs/` dir now ships in the Docker image.
- **codex-oauth `reasoning_effort` mapping** — DevinGPT's reasoning slider was being silently dropped on the codex-oauth path. Now `reasoning_effort: low|medium|high|xhigh` (top-level or in `extra_body`) maps to `reasoning.effort` in the Responses API request.

### v3.0.22 — model-supports-by-provider routing filter

DevinGPT dev team reported every `/v1/chat/completions` request being eaten by `codex-oauth` regardless of the requested model — the upstream then 400'd because Codex on ChatGPT Plus only serves `gpt-5.x` slugs. Two-part fix:

1. `select_provider` now consults `model_capabilities` for the requested model. Providers whose scanned capabilities exist and don't list the requested model are filtered out at selection time. Providers without scanned caps still get a chance (we don't know what they support; let the existing CB catch failures). If the filter would empty the candidate list, fall through to the original list rather than hard-503.
2. `/api/chat/completions` now passes the caller's `model` to `select_provider` as `model_override` even when no `ModelAlias` row exists. Previously the override was only set for aliased requests, which is why the new filter wasn't firing for the vast majority of calls.

End-to-end verification: `gpt-4o-mini` now routes to the OpenAI provider (priority 6) instead of being eaten by codex-oauth (priority 3); `gpt-5.5` still routes to codex correctly.

### v3.0.21 — API key reveal UI polish

- Create-time message changed from the alarming "Copy this key now — it will NOT be shown again" to "you can come back later, every key has a 👁 reveal button". The reveal infrastructure (Fernet-encrypted at rest, admin-gated `/api/keys/{id}/reveal` endpoint, per-row toggle) has been in place since prior versions; the create-modal copy was scaring users away from a working feature.
- Reveal row icon: swapped from `Key` to `Eye` so it visually reads as "view".
- Added a tooltip+disabled-icon hint for legacy keys created before Fernet encryption — those genuinely can't be recovered, and now the UI explains why instead of just hiding the button.

### v3.0.20 — ApiKey tombstone-aware delete (resurrection bug)

Same shape as v2.8.2's Provider tombstone fix. Previously, hard-DELETE'ing an API key on one node was reversed by the next cluster sync push from a peer that still had the row — `apply_sync` saw `existing is None` and re-INSERTed it. Test/regression keys couldn't be cleaned up; admin-deleted keys reappeared within ~60s.

Now `ApiKey` has a `deleted_at` column. The DELETE handler soft-deletes (`deleted_at = now`, `enabled = False`). Sync push includes tombstoned rows; `apply_sync` propagates peer tombstones locally and preserves local tombstones against non-tombstoned peer rows. Lookups filter `deleted_at IS NULL` (the auth path already filtered `enabled=True` so unauthorized requests were already blocked, but the admin list now hides them too). Tombstones older than `provider_tombstone_retention_days` (default 7) are hard-deleted by the daily prune sweep.

### v3.0.19 — fix codex-oauth keep-alive probe path

Same shape as v2.7.2's claude-oauth probe fix that I forgot to extend when v3.0.16 landed: the keep-alive probe was sending codex-oauth providers through `litellm.acompletion(model="openai/gpt-5.5")`, which routes to `api.openai.com` — that endpoint rejects Codex CLI bearer tokens with `"Missing scopes: model.request"`. Every 5-min probe cycle was failing → CB tripped after 3 failures → real traffic hit CB-open during the hold-down windows. Now uses the same direct dispatch path as real traffic (`chatgpt.com/backend-api/codex/responses` with the right headers + Responses API body shape), draining a streaming POST until `response.completed`.

### v3.0.18 — OAuth refresh-token race recovery

When two cluster nodes independently refresh the same OAuth provider's access_token within the 60s sync window, Anthropic and OpenAI both rotate the refresh_token on every call — whichever node loses the race gets back `invalid_grant` and would previously trip the 24h auth-failure CB until an admin manually re-pasted credentials. Now the loser fans out a signed `GET /cluster/oauth-pull/{provider_id}` to each peer; whichever peer has the freshest non-expired tokens responds, the loser adopts them locally, and the original chat call retries seamlessly. Only raises (back to the existing CB path) if no peer has fresher tokens — i.e. real upstream revocation.

Applies to both `claude-oauth` and `codex-oauth` provider types. Same HMAC-of-(node_id) auth as `/cluster/settings` for the new endpoint. +7 unit tests for the recovery paths (cluster-disabled / no-peers / picks-freshest / skips-expired / skips-unreachable).

### v3.0.17 — chain-bump priority on OAuth /exchange paths

`POST /api/providers/claude-oauth/exchange` and `POST /api/providers/codex-oauth/exchange` now call `_bump_priority_conflicts(...)` before inserting the new row, matching the standard `POST /api/providers` behavior. Without this, adding an OAuth provider at a priority already in use produced a momentary tie until the next cluster sync's `normalize_priority_ties` resolved it (60s window). Tie no longer occurs at insert time.

### v3.0.16 — codex-oauth provider + path-relative frontend

- **`codex-oauth` provider type** — OpenAI Codex CLI / ChatGPT subscription OAuth, billed to Plus/Pro/Team/Enterprise quota instead of API tokens. Mirrors the claude-oauth admin UX (Generate Auth URL → browser approval → paste callback). Full pipeline: PKCE flow → token exchange → refresh-token rotation → Chat Completions ↔ Responses API translator → request dispatch via `chatgpt.com/backend-api/codex/responses`.
- **Path-relative frontend** — `base: './'` in `vite.config.ts` plus runtime `getBasePath()` detection so a single built bundle deploys at any URL prefix. Smoke node now actually serves the SPA correctly at `/llm-proxy2-smoke/` (was previously broken — only `/health` worked).
- **Rate-limit awareness for codex-oauth** — reads `x-codex-*` headers on every successful response (plan tier, used %, reset-at, window minutes); force-opens the CB on 429 / limit-exceeded with hold-down equal to upstream's reset-after seconds. New `/api/providers/{id}/rate-limit` admin endpoint surfaces state for monitoring.
- **`scan_models` endpoint fix** — comprehension expected `list[str]` from `scan_provider_models` but it returns `list[dict]`. Latent for all provider types since v3.0.9; surfaced when codex-oauth scan returned 6 real models. `unhashable type: 'dict'` fixed.
- **OAuth edit-rotate clobber fix** — extends the v2.7.x `api_key` preservation to also cover `extra_config` (preserves the rotate endpoint's freshly-stashed `chatgpt_account_id`/`chatgpt_plan_type` against the form snapshot's PUT). Applies to both claude-oauth and codex-oauth.
- **Tests** — +10 translator + +10 ratelimit; 822 unit tests green.

### v3.0.14 — runtime model-deprecation auto-bump

When upstream returns a `NotFoundError` for a model in our `MODEL_DEPRECATIONS` registry, `acompletion_with_retry` now persists the replacement to every active provider's `default_model` and retries the same call once with the new model id. Closes the boot-time-only gap from v3.0.9 — if a vendor retires a model live mid-day, we self-heal on the first failure instead of bleeding errors until the next deploy. The bump is one retry per call (no infinite loop); if the replacement also fails, the existing CB / next-provider fallback path takes over.

### v3.0.13 — tombstone garbage collection + rolling-deploy caveat

- **Tombstone GC** — daily prune sweep now hard-deletes `Provider` rows whose `deleted_at` is older than `provider_tombstone_retention_days` (default 7, env `PROVIDER_TOMBSTONE_RETENTION_DAYS`). Closes the long-standing TODO from v2.8.2's soft-delete design. Cluster sync converges in seconds, so 7 days is a comfortable safety margin before hard-delete.
- **README** — adds the v3.0.11 mixed-version rolling-deploy caveat to the deploy section so future operators don't lose an edit during the brief upgrade window.

### v3.0.12 — provider name dedup + drop v3.0.9 backstop instrumentation

- **Boot-time dedup:** `dedup_providers_by_name` collapses duplicate-name active provider rows (cluster-sync legacy) into one survivor — keeps the highest-priority row (lowest `priority` value; ties broken by oldest `created_at`, then lowest `id`), tombstones the rest. Idempotent. Tombstone stamps `last_user_edit_at` so the dedup decision propagates as an authoritative cluster-sync edit.
- **Create/update guard:** POST `/api/providers` and PUT `/api/providers/{id}` now 409 on duplicate names. The OAuth-flow `/api/providers/claude-oauth/exchange` shares the same guard.
- **Removed v3.0.9 backstops' `logger.info` lines** for `oauth.max_tokens_default_applied` and `oauth.cc_marker_omitted` — fleet-wide scan showed zero triggers; defaults stay in place but quietly.
- **Smoke node graduation:** `/llm-proxy2-smoke/` on www01 is now a permanent pre-prod stage.

### v3.0.11 — last_user_edit_at gates cluster-sync LWW

Provider rows now carry a separate `last_user_edit_at` Unix timestamp set only by admin-facing endpoints (create / update / delete / toggle / OAuth rotate / OAuth exchange). Cluster sync prefers it over `updated_at` when both sides have one, so a peer's OAuth auto-refresh, deprecation auto-bump, or priority tie-break can't make the row look fresher than a real rename or config edit. Local edits beat peer rows that have no stamp (conservative during mixed-version rollout windows).

### v3.0.10 — cluster sync covers name + daily_budget + OAuth fields; force-sync-now endpoint

Provider sync payload was missing the `name`, `daily_budget_usd`, `oauth_refresh_token`, and `oauth_expires_at` fields — renames and budget changes on one node never reached peers. Plus an admin-only `POST /cluster/sync-now` endpoint to force convergence after a config change without waiting for the 60s loop.

### v3.0.9 — deprecation auto-bump + stale-bundle banner + dead-code instrumentation

- **`app/providers/deprecations.py`** — `MODEL_DEPRECATIONS` registry (deprecated → replacement) with current Google / Anthropic / OpenAI retirements. `migrate_deprecated_default_models(db)` runs at boot (idempotent) and bumps every provider row's `default_model` to the registered replacement. `check_model_deprecation(model)` used by `/test` and `/scan-models` response builders to surface deprecation warnings in the UI before the upstream 404s on real traffic.
- **Stale-bundle banner** — `Layout.tsx` watches first-observed `/health` version and shows a "Reload now" banner when the served app diverges (browser cache after deploy).
- **Backstop instrumentation** added to `_messages_streaming.py` for the `max_tokens` default + cache_control marker cap-check (later removed in v3.0.12 after a week of zero triggers).
- **Smoke node roll-forward** to v3.0.9 alongside the production fleet.

### v3.0.8 — refactor: SCHEMA-type fix + auth dedup + worker split

Three pure refactors — no behavior change, 799 unit tests still green.

- **SCHEMA-type structural fix** — pydantic field annotations on `app.config.Settings` are now the canonical source of setting types; `config_runtime.SCHEMA`'s `type` is a UI hint and a fallback. `_pydantic_field_type` + `canonical_type` + `validate_schema_consistency` (boot-time WARN). Closes the v3.0.1 bug class where SCHEMA said `"str"` for a float field and `_coerce` returned a string into a numeric comparison.
- **Auth dedup** — new `get_api_key_record` + `resolve_api_key_dep` factory in `app/auth/keys.py`; `app/api/runs.py` collapsed 5 raw_key extraction blocks into `Depends(_AUTH)`.
- **Worker split** — `app/runs/worker.py::_drive()` (was 250 lines) split into per-state handlers (`_step_check_deadline`, `_step_queued`, `_step_running`, `_handle_tool_use`, `_handle_terminal_text`, `_peek_next_model`, `_maybe_compact_run`, `_wait_for_rate_limit_slot`, `_fail_run`).

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
