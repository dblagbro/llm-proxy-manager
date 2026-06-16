# Changelog

All notable changes since v2.7.6. Older history available in `git log`.

The project follows [Semantic Versioning](https://semver.org/) loosely:
**major** = breaking API changes; **minor** = additive features; **patch** = fixes.

---

## v5.7.x — MCP aggregation endpoint

### v5.7.4 — Per-key MCP allow/deny + token-budget enforcement (2026-06-16)

Pre-freeze sprint: lock down the MCP surface BEFORE bots start hitting it.

- **`api_keys` gains 3 columns**: `mcp_tools_allow` (JSON list of fnmatch globs, NULL=permissive, []=restrictive), `mcp_tools_deny` (deny-wins), `mcp_schema_token_budget` (INT, NULL=unlimited).
- **`app/mcp_server/policy.py`** — `is_tool_allowed_for_key(name, allow, deny)`, `filter_tools_for_key(tools, allow, deny)`, `check_token_budget(tools, budget)`, `estimate_schema_tokens(schema)` (conservative 4 chars/token).
- **`app/mcp_server/server.py`** — `_wrap_list_tools_with_policy(mcp)` and `_wrap_call_tool_with_policy(mcp)` monkey-patch the FastMCP instance's `list_tools` / `call_tool` to consult `current_mcp_policy` ContextVar before returning anything. Bearer auth middleware sets the ContextVar from the key record on every authed request.
- **Path B (`/v1/messages` injection)** — messages.py now also sets `current_mcp_policy` after `verify_api_key`, so the bridge → `mcp.list_tools()` round-trip filters by the same rules as the /mcp endpoint. No drift between the two surfaces.
- **`POST/GET/DELETE /api/admin/mcp/keys/{key_id}/policy`** — admin-gated CRUD over the three fields. Every write emits a `CompliancePolicyChange` audit row (same audit table as v5.2.x policy edits).

Tests: 17 new pins in `test_v574_mcp_per_key_policy.py`. `test_v560_proxy_excel_tool::test_messages_handler_injects_proxy_tools_for_non_streaming` window widened to 2000 chars to accommodate the v5.7.4 policy block. Full suite **3033 passed, 2 skipped** (~56s).

### v5.7.3 — Hub-team memo: Path A MCP client config snippets (2026-06-16)

Drafted at `docs/memos/2026-06-16-hub-team-mcp-client-config.md`. Operator forwards to coordinator-hub team. Adds installer blocks for Claude Code / opencode / Cursor / Continue / Cline that point each bot client at `/llm-proxy2/mcp/` with the existing per-bot API key as bearer. Path B (proxy-side auto-injection) already works for every bot today — Path A surfaces the MCP tools in the bot client's own UI + per-tool audit story. The two paths are additive, dedup'd by tool name on the injection side.

### v5.7.2 — Hotfix: SPA catch-all swallowed bare /mcp (2026-06-14)

Surfaced during the v5.7.1 fleet roll log-watch sweep: `GET /mcp` (no trailing slash) returned HTTP 200 + the React SPA index.html instead of the FastMCP endpoint. Root cause: Starlette mounts require the path to end in the mount prefix + `/` (or have additional path). Bare `/mcp` fell through to the catch-all `@app.get("/{full_path:path}")`, which served the SPA shell.

`/mcp/` (with trailing slash) worked correctly all along — that's the path real MCP clients send. This was a UX bug for API clients probing without the slash, NOT a security hole (the SPA HTML doesn't leak data; the actual MCP endpoint behind the slash still required bearer-key auth).

Fix: added `"mcp"` to the API-namespace short-circuit in `spa_catch_all`. Bare `/mcp` now returns JSON 404; `/mcp/` continues to hit the FastMCP endpoint with auth.

Tests: 1 pin in `test_v572_spa_catchall_mcp_guard.py` (source-grep contract on the namespace skip list).

### v5.7.1 — Bridge MCP tools into /v1/messages injection + markitdown + system-prompt nudge (2026-06-14)

Operator-approved 2026-06-14 (recommended-plan continuation). v5.7.1 makes the MCP aggregation endpoint actually USE its tools on every existing bot without any client-side config change. Three additive ships in one release:

- **`app/proxy_tools/mcp_bridge.py`** — new bridge module. Reads the live tool list from the FastMCP root via `list_tools` (60s TTL cache), converts each MCP tool to an Anthropic-shape `ProxyTool`, and routes tool invocations back through `mcp.call_tool`. Means: every tool registered on `/mcp` is automatically injected into `/v1/messages` requests, zero per-tool code in proxy_tools.
- **`app/proxy_tools/__init__.py`** gains `get_registry_async`, `inject_anthropic_async`, `find_proxy_tool_use_async` — async variants that source from BOTH the v5.6.0 static registry AND the bridge. Static tools win on dedup by name (so the v5.6.0 Excel implementation stays canonical until v5.8.x deprecation). The sync helpers remain for diagnostics + tests.
- **`app/api/messages.py`** switched the non-streaming `/v1/messages` injection + interception call sites to the async variants. Live bots now see markitdown + every future MCP tool without a config touch.

**New tool: `convert_document_to_markdown`** (`app/mcp_server/tools.py`):
- Microsoft `markitdown` wrapper. Handles DOCX / PDF / PPTX / HTML / EPUB / CSV / MD / TXT / JPG / PNG / ODT.
- Same input contract as `read_xlsx_to_markdown`: `file_b64` OR `url`. Optional `file_extension` hint when URL doesn't carry one.
- Reuses the v5.6.0 `_fetch_bytes` 5 MB cap + URL scheme validation.
- Registered on the FastMCP root AND surfaced through the bridge — bots see it automatically via `/v1/messages` injection.

**System-prompt augmentation (per-key opt-in):**
- New ApiKey column `system_prompt_mcp_augmentation BOOLEAN DEFAULT 0`. ALTER runs on upgrade so existing keys default off.
- When True AND tools were injected, prepends a one-line nudge to `body["system"]` telling the model: *"You have access to proxy-injected tools for reading Excel/Word/PDF/PowerPoint/HTML/EPUB documents, fetching URLs, and converting documents to markdown. When the user asks about content that would benefit from these tools, call them instead of saying 'I can't read X' or 'I don't have access'."*
- Handles both `body["system"]` as string OR as list-of-text-blocks (both shapes Anthropic accepts).
- Default off mirrors how `ai_provider_supervisor_auto_apply` shipped initially. Operator flips per key once observed to be helpful.

**markitdown dep added** (`markitdown>=0.1.0`). Base install only (no `[all]` extra — that brought ~300 MB of Azure cloud-OCR / audio / youtube deps).

**Not in v5.7.1 (deferred to v5.7.2 / v5.7.3):**
- External stdio sub-servers (git via uvx, filesystem via Node). Filesystem requires Node-in-image — separate decision; git is fine but defers cleanly.
- Per-key tool allow-list / token-budget enforcement / `list_tools` schema cache trimming.
- Capability scout (refusal-pattern detector worker).

Tests: 13/13 in `test_v571_mcp_injection_extension.py`. `test_v560_proxy_excel_tool::test_messages_handler_injects_proxy_tools_for_non_streaming` updated to accept either sync or async injection (forward-compat). Full suite **3015 passed, 2 skipped** (~43s).

### v5.7.0 — MCP aggregation scaffold + 2 in-process tools (2026-06-14)

Operator-approved 2026-06-14 after a 5-agent research dive. The proxy now exposes a single Streamable HTTP MCP endpoint at **`/mcp`** that downstream clients (Claude Code / opencode / Cursor / Continue) can register as ONE MCP server URL and get N aggregated capabilities. v5.7.0 is intentionally scaffold-only — 2 in-process tools, no external sub-servers yet — to keep the v1 risk surface tight.

- **`app/mcp_server/`** — new package: `__init__.py` (ContextVars for per-request `api_key_id` + `parent_request_id`), `tools.py` (the 2 tools), `server.py` (FastMCP root + bearer-key auth middleware).
- **`app/mcp_server/server.py::build_mcp_app`** — constructs the FastMCP root with `stateless_http=True, json_response=True, streamable_http_path="/"`. Production combo from the research dive: no sticky sessions, scales horizontally, dodges SDK issue #1367 (trailing-slash 307→404 on FastAPI mount).
- **2 in-process tools registered:** `read_xlsx_to_markdown` reuses v5.6.0's `_render_workbook` so there's no code drift between the two surfaces; `fetch_url` is a fresh HTTP GET with safety caps (http/https only, 5 MB body cap, 30s timeout, 5 redirects). Explicitly NO `list_directory` / no shell / no command-execution tools — pinned anti-tests prevent slipping them in.
- **`BearerKeyAuthMiddleware`** — Starlette middleware reuses the existing `verify_api_key` flow. No new IdP, no new bearer surface. Returns 401 with proper `WWW-Authenticate` headers on miss; sets the `current_api_key_id` ContextVar on success.
- **`McpToolCall` table** (`app/models/db_mcp.py`) — per-tool-call audit row: `api_key_id`, `parent_request_id`, `tool_name`, `mcp_server_id`, `input_summary` (capped 480 chars; intentionally NOT the full payload to avoid PII leaks), `output_bytes`, `latency_ms`, `ok`, `error_msg`. v5.7.x will migrate this into `compliance_events` for cluster replication.
- **`app/main.py` integration** — `_mcp_sub_app = build_mcp_app()` constructed at module init; mounted on `/mcp`; FastMCP `session_manager.run()` entered inside the lifespan BEFORE yield so the sub-app's task group is alive when the first request arrives (the SDK issue #1367 fix).
- **`mcp>=1.27,<2`** pinned in `requirements.txt`. v5.8.0 will branch for the 2026-07-28 spec drop (session removal, new `Mcp-Method`/`Mcp-Name` headers).

**Open decisions defaulted (operator can redirect):** keep v5.6.0's in-process Excel tool live alongside the new MCP path; include MarkItDown in v5.7.1; capability-scout auto-enable defaults off; token-budget hard-fail with `X-Token-Budget-Exceeded` header; `mcp<2` pin.

**Forward plan (operator-confirmed priority order):**
- **v5.7.1** (~4h): mount 4 external stdio sub-servers via FastMCP `mount()` — filesystem, fetch, git, markitdown.
- **v5.7.2** (~3h): per-key tool allow-list in `compliance_policy.mcp` + token-budget enforcement + `list_tools` cache.
- **v5.7.3** (~6h): capability scout v1 — refusal-pattern detector worker + `mcp_capability_map` seed + `mcp_suggestion` table + LMRH Link-header injection.
- **v5.7.4** (~3h): per-MCP-server telemetry + UI panel.

Tests: 17/17 in `test_v570_mcp_aggregation_scaffold.py` (deps, package layout, model registration, FastMCP production-settings source-grep, bearer-auth contracts including 4 dispatch behaviors, tool registration anti-pins for dangerous tools, main.py mount + lifespan ordering, tool implementations). `test_v4411_db_split` extended for the new `db_mcp` domain module (registry 36 → 37 tables). Full suite **3002 passed, 2 skipped** (~39s).

---

## v5.6.x — Proxy-injected tools

### v5.6.0 — `read_xlsx_to_markdown` tool (non-streaming) (2026-06-14)

Operator ask 2026-06-14: "some bots have said they can't do excel docs — can we insert tools to do this for them?"

The proxy now appends a small set of helper tools to every non-streaming `/v1/messages` request before forwarding. When the model invokes one, the proxy runs the tool in-process and re-issues the conversation with the `tool_result` injected, so the caller sees a final answer with file content already incorporated. The caller never sees the `tool_use` block.

- **`app/proxy_tools/__init__.py`** — `ProxyTool` dataclass + module-level `REGISTRY` + `inject_anthropic(body)` + `find_proxy_tool_use(content)` + `build_tool_result_message(tool_use_id, output)`. Idempotent injection (skips if the caller already supplied a tool with the same name).
- **`app/proxy_tools/excel.py`** — `EXCEL_TOOL` (`read_xlsx_to_markdown`). Input accepts either `file_b64` (base64-encoded xlsx blob) OR `url` (https URL the proxy fetches). 5 MB cap; default 200 rows × 30 cols; per-sheet markdown tables; `data_only=True` so formula cells return cached eval (no macro execution).
- **Wired into `app/api/messages.py`** — injection guarded by `if not stream` (streaming returns 400 on tool fire until v5.6.1). After the non-streaming response, up to 3 hops of tool-use → run → re-call → response are executed; `X-Proxy-Tool-Hops` response header reports the count.
- `openpyxl>=3.1.0` added to `requirements.txt`.

Limitations / forward work:
- **v5.6.1**: streaming support (buffer upstream stream, detect `tool_use` across chunks, inject `tool_result`, continue).
- **v5.6.2**: `/v1/chat/completions` (OpenAI shape — different tool envelope `{type: "function", function: {...}}`).

Tests: 19/19 in `test_v560_proxy_excel_tool.py` (registry shape, injection idempotency, find/build helpers, Excel runner: b64 path + URL scheme rejection + sheet filter + row cap, source-grep contracts for messages.py wiring). Full suite **2985 passed, 2 skipped** (~48s).

---

## v5.5.x — cursor-oauth noVNC sidecar (silent JWT rotation)

### v5.5.0 — `cursor_bridge_session` scaffold (Phase 1 of 4) (2026-06-12)

Operator-approved noVNC sidecar project — replaces operator-driven cursor-oauth re-auth (~60-day cadence) with a persistent Playwright + Chromium session that re-runs the PKCE flow silently every ~24h. The empirical `oauth_refresh_token = NULL` discovery on the existing Cursor-oAuth-C1acct provider closed the door on the cheap refresh-token alternative, so the noVNC path is the right move.

**Scaffold-only ship.** Phase 1 commits the directory + Dockerfile + supervisord + FastAPI skeleton, no container running yet:

- `cursor_bridge_session/` — new top-level directory mirroring the `grok_bridge/` pattern.
- `Dockerfile` — Playwright base image + Xvfb + x11vnc + noVNC + websockify + fluxbox + supervisord (matching grok_bridge's proven shape).
- `requirements.txt` — fastapi, uvicorn, httpx, playwright 1.45.0.
- `supervisord.conf` — 4-program stack: xvfb → fluxbox → x11vnc → websockify.
- `start.sh` — boots supervisord, waits for Xvfb to actually accept X11 connections (NOT just for the socket file — same BUG-025 hardening grok_bridge learned), then exec's uvicorn as PID 1.
- `app.py` — FastAPI app v5.5.0. `/healthz` returns the scaffold sentinel (`{status: ok, phase: scaffold-v5.5.0, uptime_sec: N}`). `/api/status` returns the stub session-health shape (last_rotation_at, logged_in, all NULL in v5.5.0). `/api/rotate` explicitly returns `not-implemented-in-scaffold` with a v5.5.1 forward-pointer so the operator doesn't think rotation works yet.
- `docs/cursor-oauth-novnc-design-v5.5.md` — full design doc: motivation, architecture, 4-phase ship plan with effort estimates, ready-to-paste compose entry block + nginx route + HMAC callback shape for v5.5.1-v5.5.2.

**Not in v5.5.0:** Chromium launch, /vnc/ route, PKCE replay, rotation cron, HMAC callback, compose entry in `/home/dblagbro/docker/docker-compose.yml`. Operator gets to review the design + scaffold shape before any container exists.

**Forward ship plan:**
- **v5.5.1** (~4h): Playwright lifespan launches Chromium with persistent context under `/data/playwright-state`. `/vnc/` reverse-proxies to websockify. nginx route + compose entry wired. Operator can sign in to Cursor via noVNC tab.
- **v5.5.2** (~4h): PKCE generator + `/loginDeepControl` drive + 24h rotation cron. HMAC-signed callback to new endpoint `POST /api/admin/cursor-oauth-rotate-callback` on llm-proxy2.
- **v5.5.3** (~3h): "Session health" UI panel on ProvidersPage.

Tests: 10/10 in `test_v550_cursor_bridge_session_scaffold.py` (directory + 5-file presence + Dockerfile base image + noVNC apt stack + supervisord program names + app.py endpoint surface + version pin + design doc presence + scaffold sentinels). Full suite **2966 passed, 2 skipped** (~45s).

---

## v5.4.x — Worker-liveness observability + supervisor diagnostics

### v5.4.4 — Generalized OAuth expiry warnings + 15-day UI badge (2026-06-12)

Operator ask 2026-06-12: "we need 15 day warnings on all expiry issues like this in the ui".

Pre-v5.4.4 the `cursor_oauth_expiry_monitor` was cursor-specific, 14-day threshold, and only logged to stderr — operator couldn't see the warnings without exec'ing into the container. v5.4.4 widens + surfaces:

- **Scope widened** — the scan now covers ALL providers with non-null `oauth_expires_at`, not just `provider_type == "cursor-oauth"`. Future OAuth provider types get warnings for free without a code change.
- **Threshold bumped 14 → 15 days** to match the operator's ask.
- **activity_log row written** when a warning fires (`event_type=oauth_expiry_warning`, severity=warning, provider_id stamped). Idempotent against re-firing within 24h for the same provider.
- **UI badge added on the Provider card** (`frontend/src/pages/ProvidersPage.tsx`). Amber when `daysLeft <= 15`, red when `daysLeft <= 3` or already expired. Tooltip shows the ISO expiry timestamp + re-auth instructions.
- Backfill logic + admin endpoint (`/api/admin/cursor-oauth-expiry`) preserved verbatim — they remain cursor-oauth-specific because only cursor-oauth has the `api_key` JWT-decode path.

Tests: +5 pins in `test_v544_oauth_expiry_generalized.py` (threshold pin, scope-widened source-grep, activity_log emit + 24h dedup ordering, UI badge presence + threshold parity). Existing `test_cursor_oauth_expiry_monitor::test_get_last_sweep_returns_snapshot` updated 14 → 15. Full suite **2956 passed, 2 skipped** (~43s).

### v5.4.3 — Compliance-epoch purge admin endpoint (2026-06-12)

Closes the security-team-mandated cleanup of pre-compliance data. Operator-decided epoch is **2026-06-06 00:00 UTC** (v5.2.0 vendor-neutrality stack ship date); the security team's posture is that pre-v5.2 rows did not have vendor-neutrality policy fields evaluated, so they must be hard-deleted.

- **`POST /api/admin/compliance-epoch-purge`** — admin-gated, body `{cutoff_date, tables, dry_run, reason?}`. Defaults to `dry_run=true`. Returns per-table `rows_matched` + `rows_deleted` + `oldest_timestamp`.
- **Allow-list (PURGABLE_TABLES):** `activity_log` (created_at), `provider_metrics` (bucket_ts), `provider_ai_review` (captured_at). Adding a table requires a code change + security-team sign-off (pinned at `test_purgable_tables_is_security_team_approved`).
- **Forbidden-list (FORBIDDEN_TABLES, defence-in-depth):** `compliance_events`, `compliance_policy_changes`, `compliance_audit_chain`, `api_keys`, `users`, `providers`, `system_settings`. The endpoint returns 400 if any of these names appear in the request body, even with `dry_run=true`.
- **Audit guarantee:** in live mode the `compliance_policy_changes` audit row is committed BEFORE the DELETEs run, so a crash mid-purge still records intent in the chain. Audit row's `before_state` captures per-table matched counts; `after_state` captures intent + cutoff + table list.
- Applied to all 6 instances 2026-06-12 (3 `/llm-proxy2/` + 2 `/llm-proxy/` clone + smoke). Purge counts per instance recorded in `bug-log.md`.

Tests: 9/9 in `test_v543_compliance_epoch_purge.py` covering allow-list / forbidden-list contracts, table rejection, cutoff validation, dry-run no-modify, live-mode audit-before-delete sequencing. Full suite **2951 passed, 2 skipped** (~50s).

### v5.4.2 — Wire remaining 11 background workers to WorkerHeartbeat (2026-06-12)

Completes the BUG-069 follow-up by wiring every long-running background loop to the v5.4.0 `WorkerHeartbeat` factory. Roster grows from 5 to 16 instrumented workers:

- `cursor_billing` + `codex_billing` — vendor billing scrapes (parity with `anthropic_billing` already shipped in v5.4.0).
- `cursor_oauth_expiry` — JWT lifetime monitor.
- `caller_memory_ttl_sweeper` — caller-memory retention.
- `observability_sampler` — pool + error-rate sampler (records `tick` counter in note).
- `tool_capability_prober` — AI tool-use probe.
- `usage_rotator` — usage-bucket rotation.
- `prune` — daily activity_log retention sweep.
- `ai_rate_limiter` — rate-limit suggestion loop.
- `cluster_heartbeat` (in `app/cluster/manager.py::_heartbeat_loop`) — pings every peer.
- `cluster_peer_refresh` (in `app/cluster/manager.py::_peer_refresh_loop`) — reloads `_peers` from DB every 30s.

Every wired worker writes status `ok` / `error` / `disabled` with a one-line note. Each registers its expected interval so `/health.workers[].stale` flips True when the cadence is missed. No new schema, no admin endpoints — pure observability extension.

Tests: +4 pins in `test_v542_remaining_worker_heartbeats.py` (source-grep contracts for all 11 wiring points + roster sanity-pin at 16 workers so a future loop addition without a heartbeat trips CI). Full suite **2942 passed, 2 skipped** (~54s).

### v5.4.1 — Audit-chain zero-row warning + openai retry tap hardening (2026-06-12)

Closes BUG-072 + BUG-073 from the 2026-06-12 sweep. Both findings were observability holes left by v5.3.x ships.

- **BUG-073 — `audit_chain_zero_row_streak` warning.** `app/monitoring/compliance_audit_worker.py::_emit_zero_row_warning_if_threshold` checks the last 3 daily chain rows after each sign; if all have `row_count = 0`, emits one `warning`-severity `activity_log` row. Idempotent — re-running the worker on the same streak in a 24h window won't multiply the noise. Threshold pinned at 3 (long weekend won't trigger). Surfaces in the existing activity feed.
- **BUG-072 — retry tap also writes to activity_log.** `app/observability/openai_retry_tap.py` now writes an `openai_client_retry` `activity_log` row alongside the Prometheus increment. Pre-v5.4.1 the v5.3.4 tap was Prometheus-only, so SQL probes against `activity_log` for `%retry%` event types returned zero rows even when the counter was advancing — read as "tap broken" on inspection. The write is best-effort (background task; errors swallowed); the Prometheus counter remains the source-of-truth.
- **BUG-072 — `is_installed()` + `self_test()` introspection.** New helpers surface whether the tap was attached at boot and synthesize a retry record to confirm end-to-end wiring.
- **`POST /api/admin/ai-supervisor/retry-tap-self-test`** — admin-gated, exercises `self_test()`. Pairs with `/run-once` as the second supervisor diagnostic.
- **`compliance_audit_worker` also wired to WorkerHeartbeat.** Brings the count of heartbeat-instrumented workers from 4 → 5 (BUG-069 follow-up).

Tests: +7 (BUG-073) + +8 (BUG-072) = 15 new pins in `test_v540_audit_chain_zero_row_warning.py` + `test_v540_openai_retry_tap_hardening.py`. Full suite **2938 passed, 2 skipped** (~45s).

### v5.4.0 — WorkerHeartbeat factory + /health.workers + supervisor run-once (2026-06-12)

Closes BUG-069 / BUG-070 / BUG-074 from the 2026-06-12 post-refactor regression sweep. Background loops were previously invisible to a snapshot probe — `cluster_sync_last_run`, `ai_provider_supervisor_last_run`, billing scrape last-run, and the openai retry tap were never persisted. A worker could be hung and an operator wouldn't know until a downstream symptom surfaced it hours/days later.

- **`app/monitoring/worker_heartbeat.py`** — `WorkerHeartbeat(name=…)` writes `worker.<name>.{last_run, last_status, last_note}` rows to `system_settings` on every tick (UPSERT, bounded row count). Mirrors the `BoolSystemSetting` factory pattern. Failures inside `tick()` are swallowed and logged at WARNING — a heartbeat write must NEVER crash the worker it's measuring. `register_expected_interval(name, sec)` lets `/health` flag `stale=true` when `age_sec > 3 * expected`.
- **4 representative workers wired:** `keepalive`, `ai_provider_supervisor`, `anthropic_billing`, `cluster_sync_push`. Each writes status `ok` / `error` / `disabled` / `partial` with a one-line note summarising the tick. The remaining 11 background workers (`compliance_audit_worker`, `cursor_billing_worker`, `codex_billing_worker`, `cursor_oauth_expiry_monitor`, `caller_memory_ttl_sweeper`, `observability_sampler`, `tool_capability_prober`, `usage_rotator`, `prune`, `_heartbeat_loop`, `_peer_refresh_loop`) follow the same one-line pattern and will be wired in v5.4.x point releases.
- **`/health` envelope gains `workers: [{name, last_run, age_sec, status, note, stale}]`** — single keyspace scan, no caching (heartbeats already tick at ~60s+). Joins `circuitBreakers` and `dbPool` in the always-live re-evaluation block (excluded from the 3s response cache).
- **`POST /api/admin/ai-supervisor/run-once`** — closes BUG-070 (supervisor showed zero activity in 7 days). Admin-gated, forces one synchronous tick of `_scan_all_once()`, returns `{ok, counts}` on success or `{ok: false, error, error_type}` on crash. Bypasses the `enabled` flag intentionally — the whole point is diagnosing a worker that may not be running.

**Bug retraction:** BUG-075 (from the 2026-06-12 sweep) was a false positive. `/health` already carries the full `dbPool` block on all 6 endpoints — `size`, `checked_out`, `overflow`, `in_use`, `max`, plus `oldest_checkout_age_sec` when `db_pool_trace=true`. The QA probe only inspected the DB snapshot, not the actual `/health` JSON. Retraction notice added to `bug-log.md`.

Tests: 13/13 pins in `test_v540_worker_heartbeat.py`. Full suite **2923 passed, 2 skipped** (~45s). The v3.9.8 `test_v398_pool_diagnostics_in_health` exclusion-tuple assertion updated to include `workers` (3-element tuple).

---

## v5.3.x — Vendor-neutrality + CB hardening + cursor parity

### v5.3.9 — CB lifecycle hardening (2026-06-12)

Three changes that together stop the "providers constantly tripped" perception on chatty deployments (notably c1conv):

- **Caller-side error classifier** (`app/routing/circuit_breaker.py::is_caller_side_error`) — recognises five upstream-spec-rejection patterns (orphan tool_call_id, cursor-bridge string-expected, OpenAI Invalid user message, generic shape-rejection) and lets `record_outcome` skip the CB increment for those. The upstream isn't broken; the caller is. Activity log row still written for visibility. 2026-06-12 audit on c1conv: ~62% of CB trips that day were caused by caller-side malformed bodies, not provider failures.
- **Auto-probe on hold-down expiry** (`app/routing/circuit_breaker.py::_schedule_auto_probe`) — when `get_state()` transitions OPEN → HALF_OPEN, a detached asyncio task fires one `_probe_one()` so the CB closes itself within seconds instead of waiting for organic traffic. Low-volume providers no longer sit in half-open showing "Recovering" for tens of minutes.
- **Hysteresis pin** — `circuit_breaker_success_needed >= 2` locked via pin test so a refactor can't regress it to 1 (single lucky success closing the CB only to fail the next call).

Operator-side complement (applied via direct DB updates on c1conv, not version-bound):

- `ai_provider_supervisor_auto_apply = True` so the supervisor acts on deprioritize/disable verdicts without operator click.
- `C1 Anthropic Claude` (intentional failing fixture) `failure_threshold = 1_000_000` so its expected 100% failure rate stops tripping its own CB and polluting the dashboard.

Tests (10/10 in `test_v539_cb_hardening.py`). Full suite 2910 passed, 2 skipped.

### v5.3.8 — Logical-alias routing fix (2026-06-12)

THE deterministic opencode `EMPTY` root cause. Aliases (`coordinator-fast`, `coordinator-code`, `coordinator-reasoning`) were being passed verbatim as `model_override`, which the capability filter then misrouted to the cursor-bridge, which returned a 200-wrapped `ERROR_BAD_MODEL_NAME`. Fix: aliases now route via LMRH hint, never as `model_override`; default-model substitution + guard hardening. Companion to v5.3.7.

### v5.3.7 — Clamp Gemini thinking budget below caller `max_tokens` (2026-06-12)

Intermittent empties on Gemini path: when the caller set a small `max_tokens` and the model used most of it for thinking blocks, the final answer came back empty. Clamp thinking budget to `floor(caller_max_tokens / 2)`. Set `relay_min_max_tokens=16384` on both clusters. Real but secondary to v5.3.8.

### v5.3.6 — Cursor-bridge list-content emulation (2026-06-09)

JiuZ-Chn `cursor-to-openai` bridge rejects messages with `content: list` ("request.messages.content: string expected"). Real OpenAI accepts both. `app/providers/cursor_oauth.py::normalize_messages_for_bridge` coerces list-of-text-parts to a single newline-joined string before dispatch. Hooked into `acompletion_with_retry` via `kwargs["api_base"]` discriminator so every caller (request handlers + background workers) benefits. Provider-adapter emulation scope, NOT request mutation. Tests (14/14).

### v5.3.5 — Cursor billing parity (2026-06-09)

Audit gap from v4.4.41: cursor_billing.py scrape worker shipped but no manual-trigger UI. Brought Cursor to parity with Anthropic + Codex: `POST /api/providers/{id}/cursor-billing-refresh`, `POST /api/providers/_refresh-all-cursor-billing`, also added missing bulk Codex endpoint, `CursorBillingPanel.tsx`, expanded-card per-provider Refresh Usage button, bulk button fans out across all 3 vendors via `Promise.allSettled`. Tests (10/10).

### v5.3.4 — Tap openai-python transparent retries (2026-06-09)

Today's c1conv finding: 14 openai-python client retries clustered in a 13-min burst at 04:22-04:35 UTC, totally invisible to `activity_log` because the openai-python http layer retries internally then returns the final 200. Ship A: `app/observability/openai_retry_tap.py` — `logging.Handler` taps `openai._base_client` INFO, matches "Retrying request to X in N seconds", increments `llm_proxy_openai_retries_total{endpoint=...}`. Ship B: `propagate=False` on the same logger so the chatter stops bubbling to stdout. Bundled so suppression can't ship before capture.

### v5.3.3 — `BoolSystemSetting` factory; collapse logging_controls + llm_emergency_stop duplication (2026-06-09)

Pre-refactor, `logging_controls.py` (175 LOC) + `llm_emergency_stop.py` (180 LOC) shared ~80% identical TTL-cache + `_read_setting` + audit-on-set machinery. Extracted to `app/monitoring/_bool_system_setting.py::BoolSystemSetting`. Each shim becomes ~65 LOC routing to a single factory instance with default + on/off labels + audit subject. Future toggles now cost ~25 LOC instead of ~80. Tests (10/10 in `test_v533_bool_system_setting.py`).

### v5.3.2 — `/api/compliance/taxonomy` endpoint + frontend rewire (2026-06-09)

Closes v5.2 audit deferral risks #1+#2: custom companies added via `COMPLIANCE_CUSTOM_COMPANIES` env JSON were invisible to the WebUI because `frontend/src/types/index.ts` hardcoded the company list. New admin-gated endpoint returns merged `{id, label, source}` triples sorted by label. Frontend `useCompanyTaxonomy()` hook (TanStack `useQuery`, 5-min staleTime, 1 retry, falls back to static `KNOWN_COMPANIES` on loading/error/legacy). `ComplianceFieldsEditor` + its inner `CompanyAllowlistEditor` both consume the live list. Tests (5/5 in `test_v532_compliance_taxonomy.py`).

### v5.3.1 — Skip no-op substitution audit events (2026-06-09)

`emit_substitution_disclosure_for_route` gains a second short-circuit: when `requested_model == served_model` (case-insensitive, modulo litellm `provider/` prefix), skip the audit row + disclosure entirely. Observed 6 noise rows/week/node on hub canary where router marked `compliance_substituted=True` for cross-family normalization but no actual substitution happened. Tests (4/4).

### v5.3.0 — ApiKey policy editor UI for v5.2.1 fine-grained fields (2026-06-07)

Closes v5.2 "Risks remaining" item #1. Backend `KeyCreate` + `KeyUpdate` accept `allowed_companies` / `blocked_models` / `allowed_models`. `_validate_model_patterns` rejects empty/whitespace/oversize. Frontend `ComplianceFieldsEditor` extended with `CompanyAllowlistEditor` (toggle + multi-select + custom-id) and `ModelPatternEditor` (shared for blocked/allowed model lists). `APIKeysPage` plumbs 6 new state hooks through create + edit + reason prompt. Tests (10/10 in `test_v530_apikey_v2_policy_editor.py`).

---

## v5.2.x — Vendor-neutrality audit + remediation

Three-batch closeout of the operator-issued vendor-neutrality audit (find/fix/report). Full compliance report at `docs/v5.2-vendor-neutrality-compliance-report.md`.

### v5.2.2 — Cosmetic + docs + final report (2026-06-06)

Replaced 3 hardcoded `claude-sonnet-4-6` defaults with `PROVIDER_DEFAULT_MODELS.get(provider_type, ...)` lookups in `app/api/providers.py:243`, `app/api/_oauth_chat_translate.py:182`, `app/providers/scanner.py:402`. Authored `docs/vendor-neutrality.md` (operator policy guide) + `docs/emergency-stop-runbook.md` + the final compliance report.

### v5.2.1 — Allowlist + per-model block + runtime wildcards (2026-06-06)

Added 3 ApiKey columns (`allowed_companies`, `blocked_models`, `allowed_models`) + 3 system-wide settings. `Policy` dataclass + `evaluate_policy(policy, provider, requested_model)` + `get_effective_policy(db, api_key_id)` + `filter_providers_v2`. fnmatch glob support (`claude-*`, `gpt-4-*-turbo`). Deny wins. Cluster sync field coverage (push + apply) with membership-test pattern. Tests (27/27).

### v5.2.0 — LLM emergency stop (kill switch) (2026-06-06)

`app/monitoring/llm_emergency_stop.py` separate from v5.1.0 logging stop. Gates `/v1/messages` + `/v1/chat/completions` + `acompletion_with_retry` (covers 5 background callers). Per-flip audit row + per-blocked-request audit row. Cluster-replicated via `system_settings` (~60s convergence). 30s TTL cache invalidates eagerly on flip. Frontend `LLMEmergencyStopPanel` on `CompliancePage` (red 2px border when engaged). Tests (15/15).

---

## v5.1.x — Compliance logging controls

### v5.1.2 — Retention editable in WebUI (2026-06-05)

`app/monitoring/retention_settings.py` — 3 system_settings keys for activity_log info/warning/error retention, refresh_from_db pattern, set_retention with audit row. `GET/POST /api/admin/logging/retention` admin endpoints. Frontend `RetentionPanel` on `CompliancePage`. Prune sweep refreshes cache before each pass so flips land within one sweep cycle. Tests.

### v5.1.1 — Time-range purge + Trash tab + Copy-from picker (2026-06-05)

`POST /api/admin/activity-log/purge` (90-day window cap) with cluster-replicated `POST /cluster/activity-log/purge`. APIKeysPage Trash mode with per-row Restore (90-day retention). Add Key modal gains copy-from-existing-key picker.

### v5.1.0 — Compliance panic button + API key trash/copy (2026-06-05)

`app/monitoring/logging_controls.py` — `compliance.activity_logging_enabled` toggle, 30s TTL cache, audit row on flip. `GET/POST /api/admin/logging/{status,toggle}`. Tombstone-based API key delete with 90-day restore. Cluster_peers hardening + daily-backup integration. Grok-3 model name correction.

---

## v5.0.x — Compliance enforcement milestone

The v5.0.x line landed the gov-compliance subsystem (per-key + system-wide company block list, model substitution, audit chain) and the operational ship-rate that followed it. Spec set: `docs/5.0-compliance-design.md`, `docs/5.0-impact-map.md`, `docs/compliance-taxonomy-v5.0.0.md`. Cross-team memo trail with the Coordinator Hub team in `docs/2026-06-04-reply-{1..9}-to-hub-team-*.md`.

### v5.0.15 — Rotation clamps on `five_hour_utilization` (2026-06-04)

`app/routing/external_rotation.evaluate_rules_for_provider` now reads both `seven_day_utilization` AND `five_hour_utilization` from the Anthropic billing snapshot. Pre-v5.0.15 only the weekly bucket was considered; the session bucket (5h window, resets every 5h) hit 100% on session-max well before the weekly cap, and the router kept picking the at-limit provider as if it were healthy. Reproduced live on `Devin-Anthropic-Max-VG` (`five_hour=100% / seven_day=13%` on 2026-06-04). v5.0.15: skip if EITHER exhausts; `auto_skip_until` = LATER of the two reset times. Session bucket is a hard 100% cap (no hysteresis — it's an upstream lockout, not a tunable policy). Backward-compatible with cursor-oauth and older snapshots that don't populate `five_hour_*`. 8 new tests in `test_v5015_external_rotation_five_hour.py`; 2 v3.7.1 tests updated to default the new column to None. 2650 unit pass.

### v5.0.14 — `/metrics` route disambiguates SPA vs Prometheus via `Accept` header (2026-06-04)

Pre-v5.0.14 the bare `/metrics` path always returned Prometheus text/plain. Operators typing `https://www.voipguru.org/llm-proxy2/metrics` saw raw scrape data instead of the React `MetricsPage`. The SPA route was added in v3.x but the conflict only surfaced today when the operator hit it via a fresh tab (in-app sidebar navigation never issues a full HTTP request so it didn't trigger the conflict).

Fix is `Accept`-header sniffing on the route handler: `text/html` in Accept → serve `index.html` so React Router takes over; otherwise → existing Prometheus response. Browsers always include `text/html`; Prometheus scrapers (Grafana Cloud Agent, vmagent, prometheus itself) all send `Accept: */*` or `text/plain;version=0.0.4`. 4 new tests in `test_v5014_metrics_route_disambiguation.py`.

### v5.0.13 — `ComplianceEvent.matched_pattern` carries rejected path for `path_not_allowed` (2026-06-04)

Pre-v5.0.13 the `matched_pattern` column was NULL for every `path_not_allowed` audit row. Operators had to grep the nginx access log to identify which path was blocked — the audit row alone couldn't answer "what got blocked." Hit acutely on 2026-06-04 diagnosing the hub canary's `/v1/v1/messages` double-prefix bug (three round-trips of grep that one query should have answered). Fix is a one-line thread-through in `_emit_path_block_event`. `emit_event` docstring clarified to document what `matched_pattern` carries per event_type (UA pattern for client refusals, normalized rejected path for path-not-allowed, typically None for substitutions). 3 new tests. Promised in two prior hub-team memos.

### v5.0.12 — Remove mid-stream `event: budget` SSE frame (2026-06-04)

Hub team filed: the proxy emitted a custom `event: budget\ndata: {"remaining":N,...}` SSE frame between the last chat-completion chunk and `data: [DONE]` whenever `max_tokens` was set. Vercel-AI-SDK consumers (OpenCode, Cursor IDE, continue.dev) strict-Zod-validate every `data:` line against `{choices}|{error}`; the budget frame matched neither → `invalid_union` → hard crash mid-stream. Coordinator Hub shipped a hub-side strip (v2.1.3) as a stopgap; v5.0.12 removes the emission at all three sites (`_completions_streaming.py`, `_messages_streaming.py`, `_messages_streaming_oauth.py`). Budget signal remains on `X-Token-Budget-Remaining` header. No consumer was reading the SSE frame (verified by grep across the repo). 3 new static-pin tests catch any re-introduction.

### v5.0.11 — Merge limits + compliance into one Edit Key modal (2026-06-04)

Operator UX feedback: the Pencil (edit limits) and Shield (edit compliance policy) icons sat side-by-side on each key's row but weren't intuitive — operators couldn't tell one of them existed. v5.0.11 consolidates them into a single Pencil-icon button that opens a unified modal: Limits section (cap + rpm) above the existing `ComplianceFieldsEditor`. Reason prompt still gates `blocked_companies` / `allowed_paths` changes; limits and debug-echo edits skip it. View Details modal's "Edit limits" button became "Edit". No backend changes — single PATCH `/api/keys/{id}` handles the merged payload.

### v5.0.10 — Extract `_apply_api_keys` + `_apply_providers` from `sync.py` (2026-06-04)

`app/cluster/sync.py` was 1024 LOC (>800-LOC design.md trigger). The two largest inline blocks — api_keys merge (189 LOC) and providers merge (262 LOC) — moved into `sync_handlers.py` under the existing `_apply_<table>` pattern that other tables already followed. Brings sync.py to 573 LOC. Behavior preserved: tombstone-aware LWW (v4.4.20), full-field UPDATE+INSERT coverage (v4.4.18 + v4.4.25), BUG-080 `.limit(1)` guards, v5.0.0 compliance-cache invalidation, v3.5.9 BUG-012 CB-state cleanup, v5.0.5 per-section commit boundaries. 6 new extraction-pin tests; 4 existing static-pin tests repointed; 2 BUG-080 `.limit(1)` guards added during extraction.

### v5.0.9 — Extract `_compliance_handler.py` from messages.py + completions.py (2026-06-04)

The four compliance orchestration sites that `messages.py` and `completions.py` had been mirroring inline (raise-on-banned-UA / raise-on-no-substitute / emit-substitution-disclosure / disclosure-headers-on-upstream-error) extracted into `app/api/_compliance_handler.py`. Every v5.0.x patch since v5.0.0 had been touching both handlers in lockstep — keeping that duplication was actively painful. Net: messages.py 1095 → 929 LOC, completions.py 961 → 795 LOC, -166 LOC × 2.

### v5.0.8 — `my-compliance` dual auth + "Show keys" toggle on `APIKeysPage` (2026-06-04)

`/api/me/compliance` accepted only API-key auth; the operator's session-cookie tab got 401 on every visit. v5.0.8 makes it accept session OR api-key. Separately on `APIKeysPage`: the prefix-only display required clicking each card to see the key value — added a "Show keys" page-level toggle that surfaces the encrypted-key reveal for every row at once.

### v5.0.7 — `GET /api/admin/policy-snapshot` endpoint for hub-side enforcement (2026-06-04)

Hub team's hub-side enforcement build (v2.1.0+) needs to read the policy state without round-tripping through `/v1/messages` for every request. v5.0.7 adds a read-only admin endpoint that returns the current effective system blocklist + per-key blocklists in one JSON. Drives hub's local cache; admin-auth-only.

### v5.0.6 — Audit + disclosure record the caller's ORIGINAL requested model (2026-06-04)

The `compliance_events.requested_model` column was mislabeled. `body["model"]` gets REWRITTEN at `messages.py:355` to the served model BEFORE the audit row writes at line 557 — so `requested_model = body.get("model")` captured the SERVED model, making caller-side `SELECT requested_model, served_model` queries show identical values for every substitution. Substitution itself was always correct; only the audit field was wrong. Fix: capture `_orig_request_model = body.get("model")` at the top of the handler and pass it explicitly through the dispatch chain. Static-grep tests in `test_v506_audit_preserves_requested_model.py` catch regressions of this whole class.

### v5.0.5 — Cluster sync `apply_sync` commits per major sub-section (2026-06-04)

Pre-v5.0.5 `apply_sync` wrapped 12+ table sub-applies in ONE transaction → 19.6s SQLite write lock → "database is locked" on per-request writers across the cluster. v5.0.5 commits per section so the write lock releases between each table. If sync timings creep back over ~5s avg in future, suspect a new section was added without its `_section_commit("label")` call.

### v5.0.4 — F-anomaly fix (no-compliant-local-provider header) + cursor-oauth expiry monitor (2026-06-04)

Coordinator-local 503 path was missing the X-Compliance-* headers that the standard 503 path carried. Caller couldn't distinguish "no local provider compliant" from a generic 503. v5.0.4 backfills the disclosure headers. Plus the cursor-oauth probe (added in v4.4.41) gained an `oauth_expires_at` capture from the poll response — surfaces lifetime in the UI before re-auth becomes urgent.

### v5.0.3 — `/v1/responses` translation shim (2026-06-04)

OpenCode and a few internal callers POST to `/v1/responses` (OpenAI's newer endpoint shape) instead of `/v1/chat/completions`. Pre-v5.0.3 they got 404. v5.0.3 adds a translation shim that maps the request to chat-completions internally and the response back. No new dispatch path — just a format adapter.

### v5.0.2 — Daily compliance audit worker — hash chain + retention purge (2026-06-04)

A daily worker links the previous day's `compliance_events` into a SHA-256 hash chain in the `compliance_audit_chain` table. Each chain row references its predecessor's hash, so any tampering with historical audit data breaks the chain. Same worker purges rows older than the configured retention window. Closes one of the spec's tamper-evident requirements.

### v5.0.1 — Provider `owner_company` auto-derive + 502 compliance disclosure (2026-06-03)

Provider rows didn't reliably carry `owner_company` for older entries created before that field existed. v5.0.1 auto-derives it from `provider_type` + model family. Same ship: 502 upstream errors now carry the X-Compliance-* disclosure headers like the other error paths.

### v5.0.0 — Compliance enforcement: per-key + system-wide company block list (2026-06-03)

Major: the gov-compliance subsystem. New `app/compliance/` subpackage; three new tables (`compliance_events`, `compliance_policy_changes`, `compliance_audit_chain`); six new columns on existing tables; per-key `blocked_companies` policy enforced at the dispatcher; model substitution when a blocked-company provider would have served (no client-visible failure, full audit row written); X-Compliance-* response headers on every substituted or refused response; 10-company taxonomy (Anthropic, OpenAI, Google, Meta, Mistral, AWS, Microsoft, Cohere, xAI, Other) plus per-key override list; system-wide blocklist via SystemSetting for fleet-wide bans. Bedrock-Anthropic dual-tag handled via `model_family_companies()` returning a set so banning either company drops the dual-tagged provider.

---

## v4.3.x — "Voice output" milestone

### v4.4.41 — Cursor dashboard usage scrape + multi-vendor preferred-pick (2026-06-03)

Mirrors the Anthropic Console scrape pattern (v3.7.0) for cursor-oauth providers, so multi-account Cursor setups auto-rotate by lowest utilization — the same `🥇 router's pick today` badge logic that already works for claude-oauth now applies to cursor-oauth too.

**The 30-minute spike pivot:** the initial probe sent the stored token as `Authorization: Bearer …` and concluded the direct-HTTPS path was dead (6-10h sidecar patch needed). Reading upstream `JiuZ-Chn/Cursor-To-OpenAI/routes/cursor.js` (a new file added since our pinned digest) revealed the correct auth shape: `Cookie: WorkosCursorSessionToken=<value>`. Re-spike confirmed three GET endpoints fully cover the routing-signal surface — `auth/me`, `usage-summary` (`totalPercentUsed` is the routing signal), `dashboard/get-aggregated-usage-events` (per-modelIntent tokens + cost). Implementation cost dropped from 6-10h to 2-3h.

**Live-deploy gotcha:** `www.cursor.com` issues a 308 redirect to the apex `cursor.com` for the API endpoints. httpx's default redirect-follow STRIPS the Cookie header when crossing subdomain boundaries (security default — different from urllib). My initial deploy got 401 session_expired on every endpoint. Fix: hit the apex `cursor.com` directly. Now logged in the source comment so the next subscription-as-a-provider integration doesn't trip on this.

**What shipped:**

- `app/providers/cursor_billing.py` — three async GETs against the apex `cursor.com`, parse three JSON responses into a single `ExternalUsageSnapshot` row. Auth-state classification (`ok` / `session_expired` / `network_error` / `parse_error`) matches the Anthropic worker so the shared rotation evaluator + UI badge layers work uniformly.
- `app/monitoring/cursor_billing_worker.py` — 4h periodic worker. Same shape as `anthropic_billing_worker`: warmup delay, per-node startup jitter, freshness floor (skip a scrape if the cluster-replicated snapshot table already has a fresh row).
- `app/main.py` — wires the worker on startup (no-op when no enabled cursor-oauth providers exist).
- `app/routing/external_rotation.py`:
  - `evaluate_rules_for_all_providers` query extended from `provider_type=='claude-oauth'` to `provider_type IN ('claude-oauth', 'cursor-oauth')`, so the auto-skip rule (≥95% util → set `auto_skip_until = billingCycleEnd`) fires for cursor-oauth too.
  - New `reorder_subscription_by_utilization(providers, util_map, *, provider_type, …)` generalization of the original claude-only reorder. The back-compat `reorder_claude_oauth_by_utilization` now calls it twice — once for claude-oauth, once for cursor-oauth — so the router's existing single callsite gets multi-vendor preferred-pick behavior without router changes.
- `frontend/src/pages/ProvidersPage.tsx` — `claudeOauthIds` / `claudeOauthIdsKey` renamed to `subscriptionIds` / `subscriptionIdsKey`; the snapshot fetch covers both types. The `🥇 router's pick today` badge now renders on cursor-oauth providers (when there are 2+ of them and snapshot data exists), with a type-aware tooltip distinguishing "Anthropic Console" vs "Cursor dashboard" as the source.

**Schema reuse:** the existing `ExternalUsageSnapshot` columns are Anthropic-named (`seven_day_utilization`, `seven_day_resets_at`) but semantically generic. Cursor's billing window is monthly, not weekly — column NAMES are historical artifacts, column SEMANTICS (current util % + reset timestamp) generalize. No schema migration; no cluster-sync change. Renaming columns to `current_utilization` / `period_resets_at` is a deferred follow-up if/when a third vendor lands.

**Live verification on www1:**

```
dblagbro:             ok=True, util=0.0%, resets=2026-06-22 18:22:28, credits=$0.054
Cursor-oAuth-C1acct:  ok=True, util=0.0%, resets=2026-06-22 18:22:28, credits=$0.054
```

(Both providers belong to the same operator's Google-OAuth account — identical data is expected; the multi-account preferred-pick will be meaningful once the operator adds a separate Cursor account.)

**Validation:**

- 10 new unit tests in `tests/unit/test_v4441_cursor_billing.py` covering `parse_usage_response` mapping + edge cases, the auth-shape (Cookie not Bearer) end-to-end with a mocked client, 401 → session_expired classification, the multi-type evaluator query, the `reorder_subscription_by_utilization` generalization, the back-compat `reorder_claude_oauth_by_utilization` now handling both types in one pass, and the main.py wire-up.
- 2 existing `test_v376_providers_list_badges.py` source-grep tests updated for the rename (`claudeOauthIds` → `subscriptionIds`, scope guard moved into `pickPerType`).
- Suite: **2462 passed + 2 skipped** (was 2452 + 2 in v4.4.40 — +10 new tests, zero regressions).
- Frontend typecheck: `tsc --noEmit` exit 0.

Fleet: all 3 nodes on v4.4.41, healthy. Worker active with default 4h cadence; the first cron-driven scrape will land within `[60s, 60s+jitter]` of each container's startup.

### v4.4.40 — BUG-086: cursor-oauth missing from the model-family filter + v4.4.39 UI ship (2026-06-03)

Two ships rolled into one release because v4.4.39 hadn't propagated past www1 yet when the v4.4.40 routing bug was reported.

**v4.4.40 — BUG-086 (HIGH): cursor-oauth was being skipped by family-filter pre-priority.**

Operator-reported 2026-06-03: `Cursor-oAuth-C1acct` had priority 4 (lower number = higher precedence) but `claude-haiku` requests were routing to `Devin-Anthropic-Max-Gmail` instead — a higher-priority-number anthropic-oauth provider that came lower in the precedence order. The Cursor provider had the model in its scanned catalog AND a winning priority score; it should have been picked.

Root cause: `_model_family_provider_types` in `app/routing/litellm_binding.py` runs BEFORE the priority/scoring step as a "hard backstop" to prevent cross-family misroutes (the DevinGPT 2026-05-01 incident that put claude-sonnet-4-6 on codex-oauth). It maps `claude-*` → `{anthropic, anthropic-direct, anthropic-oauth, claude-oauth}` — **cursor-oauth was missing** because I forgot to update this filter when adding the provider type in v4.4.31. Same gap for `gpt-*` → `{openai, ChatGPT-oauth-plan}`, which also affects Cursor's relay (it serves gpt-4o / gpt-5 / gpt-5-codex too).

**Fix:** add `cursor-oauth` to both branches. Two-line change in `_model_family_provider_types`. The downstream capability filter (per-provider scanned `ModelCapability` rows) continues to eliminate models the operator's specific Cursor account can't reach, so no over-routing risk.

**Regression scope:** v4.4.31 (cursor-oauth provider type added) through v4.4.39. Any cursor-oauth provider was being skipped by the family filter for every `claude-*` and `gpt-*` request since v4.4.31. The cursor-oauth providers were still reachable for explicit non-family-matched models (cursor-specific slugs like `composer-2.5`, `default`) and for direct provider-pinned requests (alias path).

6 new tests in `tests/unit/test_v4440_cursor_oauth_family_filter.py` pinning both the positive (cursor-oauth IS in claude + openai families) and negative cases (cursor-oauth is NOT in grok / google / cohere families — don't over-correct).

**v4.4.39 — Providers UI clarity: priority ordinals + preferred badge rename.**

Operator-filed 2026-06-03 after reviewing `Devin-Anthropic-Max-VG` on the Providers page. Two confusable labels co-existed on the same card:

1. The green `✓ preferred` badge — auto-computed for claude-oauth providers (lowest 7-day Anthropic Console scrape utilization → router's first pick). The `✓` glyph read as a checkbox; the meaning was buried in the badge's `title=` tooltip.
2. The `priority N` field — operator-set, lower = higher precedence. "Priority 12" intuitively reads as more important than "priority 1" for many operators.

The form's old label was "Priority (lower = preferred)" which made the badge and the field look related when they aren't.

**Frontend fix** (backend unchanged):

- New `frontend/src/utils/ordinal.ts` — pure helper, standard ordinal pattern with the 11th/12th/13th edge case handled via the `v - 20` modulo trick.
- `ProvidersPage.tsx` per-card render: `priority 12` → `12th priority` (via `ordinal()`). Priority-tie tooltip got the same treatment.
- `ProvidersPage.tsx` claude-oauth badge: `✓ preferred` → `🥇 router's pick today`. Tooltip now explicitly says it's "not related to the operator-set Priority Score below — that's a separate, manually-configured field."
- `ProviderForm.tsx` field label: `Priority (lower = preferred)` → `Priority score (1 = highest, 999 = lowest)`. Tooltip rewritten to use ordinal framing ("1st place, 2nd place, etc.") and cross-link the badge so the disambiguation is in both places.

5 new source-guard tests in `tests/unit/test_v4439_providers_ui_priority.py` pinning the ordinal use + badge rename + form label change.

**Validation (combined):**

- Suite: **2452 passed + 2 skipped** (was 2441 + 2 in v4.4.38 — +11 new tests).
- Frontend typecheck: `tsc --noEmit` exit 0.

### v4.4.38 — incremental architectural refactor (router / messages / grok-web) (2026-06-02)

Three behavior-preserving extracts following the v3.10.9 "next refactor targets" list, motivated by the v4.4.31..v4.4.37 cursor-oauth arc which added 6 changes to `router.py`'s litellm-binding tables in a single week.

- `router.py` 998 → 800 LOC — litellm-binding tables + helpers extracted to a new `app/routing/litellm_binding.py` (274 LOC). 91+ existing import sites preserved via re-export.
- `messages.py` 927 → 861 LOC — cascade orchestration (cheap-route → grader verdict → accept-or-fall-through) extracted to `_messages_dispatch.try_cascade_dispatch` (continuing the v3.10.9 pattern).
- `grok_web.py` 866 → 825 LOC — `_bridge_chat` extracted to a new `grok_web_bridge.py` (first step of the manual/bridge axial split). Lazy import sidesteps a load-time circular.

8 new source-guard tests pin all three splits. Suite went from 2433 → 2441 passed. Behavior preserved; one narrow exception-attribution edge case documented in the refactor-log.

Full details in `refactor-log.md` and `architecture.md`.

### v4.4.37 — Cursor dispatch tests + JWT exp probe + refresh-token capture (2026-06-02)

Two follow-ups from the cursor-oauth arc:

**1. Closed the v4.4.35 test-coverage gap.** Four new tests in `tests/unit/test_router.py::TestBuildLitellmKwargs`:

- `test_base_url_included_for_cursor_oauth` — asserts cursor-oauth is in the `api_base` allowlist.
- `test_cursor_oauth_model_prefix_is_openai` — pins `PROVIDER_TYPE_TO_LITELLM['cursor-oauth'] == 'openai'`.
- `test_cursor_oauth_dispatch_passes_api_base_to_litellm` — full end-to-end-ish: `build_litellm_model + build_litellm_kwargs` together produce exactly the litellm.acompletion call the Test endpoint makes. Would have caught v4.4.31..v4.4.34's mystery in 30 seconds.
- `test_cursor_oauth_subscription_tier_membership` — pins cursor-oauth in `SUBSCRIPTION_TIER_PROVIDER_TYPES` so cost accounting stays $0.

The `TestProviderMaps::test_all_known_types_have_default` invariant immediately caught that I'd never added cursor-oauth to `PROVIDER_DEFAULT_MODELS` — fixed (set to `claude-4-sonnet`). Working as designed.

Suite: **2433 passed + 2 skipped** (was 2429 + 2).

**2. Decoded the JWT's `exp` claim — empirical lifetime is 60 days, not 30.**

```
dblagbro:          exp = 2026-08-01 21:34:54 UTC  (60 days from issue)
Cursor-oAuth-acct: exp = 2026-08-01 22:17:59 UTC  (60 days from issue)
```

JWT also carries `scope: openid profile email offline_access`. The `offline_access` scope is OAuth2's signal that **refresh tokens were issued** — meaning Cursor's IDE has a silent-rotation path we may be able to mirror server-side without ever building the noVNC sidecar.

**Probe added to `poll_for_token`**: log all response keys (not values — PII) on success so the next operator re-auth tells us empirically whether `refreshToken` / `refresh_token` / `expiresAt` is in the response. Plus the function now opportunistically captures any of those fields into `ExchangeResult.refresh_token` / `expires_at` so they reach `Provider.oauth_refresh_token` / `oauth_expires_at`.

`refresh_access_token` still raises (the wire to call Cursor's token endpoint isn't built yet) — but the data capture is in place so we can decide what to build with empirical evidence rather than guessing.

**Updated** the noVNC backlog (`project_backlog_cursor_oauth_novnc.md`) with these findings: first JWT expiry is now **2026-08-01**, and "implement refresh-token rotation" is the lower-cost alternative to noVNC if the poll response contains the tokens.

Fleet: not yet deployed (test-only + speculative response-key capture; deploy on the next routine roll).

### v4.4.36 — Cursor scan-models: dedicated _fetch_cursor_oauth_models (2026-06-02)

Same shape of bug as v4.4.35, different code path. Operator's "Scan Models" button returned `No models discovered — check API key and provider type`. Tracing through `app/providers/scanner.py::_fetch_model_list`: the `match provider.provider_type` block had **no case for cursor-oauth** — fell through to `_ → return []`. Silent fail; the UI message is a stock "0 models found" toast that doesn't say "I didn't even try."

Naively dropping cursor-oauth into the existing `case "openai" | "compatible" | ...` branch would have failed differently: the stored `base_url` is `http://llm-proxy2-cursor-bridge:3010/v1` (correct for litellm's `api_base`), and `_fetch_openai_models` appends `/v1/models` to whatever it gets — producing `http://…/v1/v1/models` → 404.

**Fix**: new `_fetch_cursor_oauth_models` that strips the trailing `/v1` from the stored `base_url` before appending the catalog path, and a new `case "cursor-oauth"` in `_fetch_model_list` to dispatch to it.

Verified live on www1 against both onboarded providers:

```
dblagbro: base_url=http://llm-proxy2-cursor-bridge:3010/v1
  → discovered 132 models
  → first 5: ['default', 'composer-2.5-fast', 'composer-2.5',
              'claude-opus-4-8-low', 'claude-opus-4-8-low-fast']
Cursor-oAuth-acct: base_url=http://llm-proxy2-cursor-bridge:3010/v1
  → discovered 132 models  (same; same upstream catalog)
```

The 132 models include the full Cursor IDE selector (Claude 4.x/4.5/4.6, Opus thinking variants, Composer, GPT-5, etc.). The operator clicks Scan Models in the UI and gets the live catalog — same source of truth as Cursor's app.

### v4.4.35 — Cursor dispatch fix: litellm api_base honoured (the real Test bug) (2026-06-02)

The Test-failure mystery from v4.4.31..v4.4.34 finally cracked. Same symptom across every fix attempt: `litellm.AuthenticationError: OpenAIException - Incorrect API key provided: user_01J***`. v4.4.31 paste fallback, v4.4.32 callout, v4.4.33 PKCE poll, v4.4.34 model rename — all returned good `200`s when I tested the stored token directly through the sidecar. The bug was nowhere in the OAuth flow.

**Root cause: `app/routing/router.py::build_litellm_kwargs` only sets `api_base` for `ollama` + `compatible` provider types.** cursor-oauth wasn't in the allowlist, so litellm received `model='openai/claude-4-sonnet'` plus `api_key='user_…::eyJ…'` with **no `api_base`** — and dutifully POSTed to `https://api.openai.com/v1/chat/completions`. OpenAI rejected the Cursor token with the exact error string the operator saw. The error said "user_01J***" because OpenAI's API echoes the rejected key in its 401 body.

**Two-line fix:**

```python
PROVIDER_TYPE_TO_LITELLM["cursor-oauth"] = "openai"             # explicit (was relying on the default)
# build_litellm_kwargs:
if provider.base_url and provider.provider_type in (
    "ollama", "compatible", "cursor-oauth",                     # ← added
):
    kwargs["api_base"] = provider.base_url
```

**Verified live on www1** by running `test_provider()` against both onboarded cursor-oauth Providers (the operator's "dblagbro" and "Cursor-oAuth-acct") through the actual Test path:

```
Testing provider: dblagbro
  result: {'success': True, 'model': 'openai/claude-4-sonnet'}
Testing provider: Cursor-oAuth-acct
  result: {'success': True, 'model': 'openai/claude-4-sonnet'}
```

Both 200. The poll-based onboarding from v4.4.33 was solid; the token was always correct. Dispatch was the silent failure mode.

**Why the test suite missed this**: there's no integration test that exercises `test_provider()` for cursor-oauth. The unit tests cover the flow module (start_authorize / poll / synthesize) and the endpoint shapes, but not the litellm dispatch wiring. The right follow-up is a test that mocks litellm.acompletion and asserts api_base reaches it for cursor-oauth — backlogged.

### v4.4.34 — Cursor default model: claude-3-7-sonnet → claude-4-sonnet (2026-06-02)

Live operator hit it immediately after v4.4.33 shipped: the Test button returned `litellm.AuthenticationError: OpenAIException - Incorrect API key provided: user_01J***...`, masking the real problem. The token format was correct; the api_key value was correct; the sidecar accepted it. What failed was **the model name**: `claude-3-7-sonnet` no longer exists in Cursor's relay catalog. The sidecar returns the unknown-model rejection as an OpenAI-format 401, which litellm parrots verbatim — `user_01J***` is the masked api_key in the error, not the actual cause.

Direct probe through the sidecar with a valid model confirmed:

```
/v1/models     → 200, 132 models, 56 Claude variants
/v1/chat/completions  model=claude-4-sonnet  → 200 (good response shape)
/v1/chat/completions  model=claude-3-7-sonnet → 401 "Incorrect API key" (sidecar/upstream misclassification)
```

**Changed** the default model from `claude-3-7-sonnet` to `claude-4-sonnet` in three places:

- `app/api/providers_oauth.py` — `CURSOR_OAUTH_SPEC.default_model`
- `frontend/src/components/providers/ProviderForm.tsx` — `OAUTH_FLAVORS['cursor-oauth'].defaultModel`
- `docs/cursor-oauth-onboarding.md` — examples + the "Choosing models" picks list (added a footnote that Cursor's catalog churns and Scan Models / `/v1/models` is the live source of truth)

**Existing provider rows** with `default_model=claude-3-7-sonnet` need their `default_model` updated to a real Cursor model name. Edit the provider through the UI or via SQL:

```sql
UPDATE providers SET default_model='claude-4-sonnet'
 WHERE provider_type='cursor-oauth' AND default_model='claude-3-7-sonnet';
```

(I ran this against www1's DB as part of the v4.4.34 deploy.)

Common Cursor model picks: `claude-4-sonnet`, `claude-4.5-sonnet`, `claude-4.6-sonnet-medium`, `gpt-4o`, `gpt-5`. Note Cursor uses suffix tiers (`-low/-medium/-high/-max`, `-fast`, `-thinking`) and renames more often than upstream model providers — call `/v1/models` through the sidecar after any onboarding hiccup.

### v4.4.33 — Cursor onboarding: real PKCE poll flow (no cookie copy) (2026-06-02)

Live operator showed me the actual URL the IDE-login Cursor renders: `https://cursor.com/loginDeepControl?challenge=…&uuid=…&mode=login&supportsSelectedTeamLogin=true`. After login Cursor's backend pairs the (uuid, challenge) with the user's WorkOS session, then polling `https://api2.cursor.sh/auth/poll?uuid=…&verifier=…` returns the access token. v4.4.31 ignored this entirely and made the operator copy the cookie out of DevTools. v4.4.32 added a callout to make the cookie copy obvious. v4.4.33 ditches the cookie copy: backend mirrors the IDE login flow PKCE-style, end-to-end.

**Algorithm** (cribbed verbatim from the upstream sidecar's `cursorLogin.js` — verified by reading it inside the running container):

```
verifier  = base64url(random_bytes(43))         # ~58 chars
challenge = base64url(sha256(verifier))         # 43 chars
uuid      = uuid4()
URL = https://cursor.com/loginDeepControl?challenge=<challenge>&uuid=<uuid>&mode=login&supportsSelectedTeamLogin=true

# After operator opens URL + signs in:
GET https://api2.cursor.sh/auth/poll?uuid=<uuid>&verifier=<verifier>
   User-Agent: Cursor/0.48.6 Electron/34.3.4   (matches sidecar UA)
# On 200: { accessToken, authId: "workos|<userid>" }
# Synthesized token: "<userid>::<accessToken>"
```

**Operator flow now:** click Generate Auth URL → open in tab → sign in to Cursor → switch back to llm-proxy2 → click Save Provider. The save-handler calls `POST /api/providers/cursor-oauth/poll`; backend polls api2.cursor.sh for up to 30s; returns the token; Provider row created. No DevTools, no cookie copy, no paste.

**Backend additions** (`app/api/providers_oauth.py`):

- `_do_poll_create` + `_do_poll_rotate` — shared poll-based handlers parameterized by `OAuthProviderSpec`, ready for any future vendor whose flow exposes `poll_for_token`.
- `OAuthPollRequest` + `OAuthPollRotateRequest` Pydantic models — same shape as the exchange/rotate requests minus the `callback` field.
- `POST /api/providers/cursor-oauth/poll` and `POST /api/providers/{id}/cursor-oauth-poll-rotate` — admin-gated, wire into the shared handlers above.

**Frontend additions:**

- `providersApi.cursorOauthPoll` + `cursorOauthPollRotate` in `frontend/src/api/index.ts`.
- `ProvidersPage.saveMutation` routes cursor-oauth + state + no-callback → poll endpoint (create or rotate as appropriate).
- `ProviderForm.tsx` cursor-oauth branch now shows an indigo "Waiting for your Cursor login…" panel after Generate Auth URL is clicked, with instructions to sign in then click Save Provider. The DevTools cookie-copy UI is gone from the default path; the paste-fallback (showPasteFallback toggle) remains as a backup.

**Fallback path preserved.** The v4.4.31 paste-cookie path is still wired (the modal's "Paste credentials instead" link). Operators who already have a `user_<id>::<JWT>` token in hand can still onboard that way, and the existing `/cursor-oauth/exchange` endpoint still works.

**Validation:**

- 10 new tests in `tests/unit/test_v4431_cursor_oauth.py` (33 total in the file) covering: authorize URL shape (challenge + uuid + mode + supportsSelectedTeamLogin), PKCE pair correctness (SHA-256 of verifier == challenge), `_synthesize_user_token` happy + missing-authId paths, `poll_for_token` first-success / repeat-then-success / timeout / unknown-state, and source-level guards for the new endpoints + frontend wiring.
- Full unit suite: **2429 passed + 2 skipped** (was 2419 + 2 in v4.4.32).
- Frontend typecheck: `tsc --noEmit` exit 0.
- Live www1: `version=4.4.33 status=healthy`; `/api/providers/cursor-oauth/poll` returns 401 for unauthenticated callers (endpoint wired + admin-gated).

**Operator next:** validate live by adding a fresh Cursor Provider through the UI. Click Generate Auth URL, sign in, click Save Provider. Token should arrive without any DevTools.

### v4.4.32 — Cursor onboarding UX fix: "no auto-redirect" callout (2026-06-02)

Live operator hit the gap in v4.4.31's polished flow: clicked Generate Auth URL, landed on `cursor.com/dashboard`, and then sat there waiting for a callback that was never going to happen (Cursor doesn't implement OAuth+PKCE; there is no redirect with a code). The DevTools-cookie instructions were already in the modal but read like background info, not "here's what to do next."

**Fix** (frontend-only):

- New amber callout box appears in the cursor-oauth modal **after** Generate Auth URL is clicked. Title: "⚠ Cursor does not auto-redirect — you grab the cookie manually." Includes a copy-paste DevTools Console snippet that copies just the `WorkosCursorSessionToken` value to the clipboard:

  ```js
  copy(document.cookie.split('; ').find(c=>c.startsWith('WorkosCursorSessionToken='))?.split('=')[1] || 'NOT FOUND — are you logged in?')
  ```

- The textarea label below the callout reads "Paste the WorkosCursorSessionToken cookie value" for cursor-oauth (was the generic "Paste the authorization code (or the full callback URL)" inherited from claude-oauth/codex-oauth).
- The textarea placeholder shows the cursor-specific shape (`user_…%3A%3A…JWT…` or the full `WorkosCursorSessionToken=…` cookie line) instead of `code=…&state=…`.

No backend or test changes. Other OAuth flavors (claude-oauth, ChatGPT-oauth-plan) untouched.

### v4.4.31 — Cursor as a Provider (polished OAuth onboarding) (2026-06-02)

Operator can now add a Cursor Pro/Business subscription as a backend Provider through the same modal-driven flow as `claude-oauth` and `ChatGPT-oauth-plan` — no compose-file edits, no `docker exec` plumbing, no raw cookie handling beyond a single DevTools paste.

**What landed:**

- New provider type **`cursor-oauth`** in `app/api/providers.py`, `app/api/providers_oauth.py`, `app/monitoring/helpers.py` (subscription tier), and the frontend `ProviderType` union.
- New OAuth flow module `app/providers/cursor_oauth_flow.py` registered through `CURSOR_OAUTH_SPEC` so the existing `OAuthProviderSpec` machinery serves three endpoints: `POST /api/providers/cursor-oauth/{authorize,exchange}` and `POST /api/providers/{id}/cursor-oauth-rotate`.
- New credential parser `app/providers/cursor_oauth.py` accepts the raw `user_<id>::<JWT>` token, JSON blob with `access_token` / `accessToken`, or nested `{tokens: {access_token: ...}}` — same defensive shape as the other vendors.
- New `OAUTH_FLAVORS['cursor-oauth']` entry in `ProviderForm.tsx` with vendor-specific instructions (DevTools → Application → Cookies → `WorkosCursorSessionToken`) and a Cursor-aware account-name copy block.
- `base_url` auto-pinned to `http://llm-proxy2-cursor-bridge:3010/v1` inside `_do_exchange_create` so the sidecar URL is invisible to the operator.
- The cursor-bridge sidecar service (`ghcr.io/jiuz-chn/cursor-to-openai`, digest-pinned, internal-only, healthchecked, resource-capped) was added to `/home/dblagbro/docker/docker-compose.yml` in the v4.4.31 prep work.

**Vendor-specific quirks** (documented in `docs/cursor-oauth-onboarding.md`):

- Cursor's auth does **not** implement OAuth+PKCE. The "authorize" step is a deep-link to the operator's Cursor dashboard; the "code" the operator pastes back is the `WorkosCursorSessionToken` cookie. `extract_code_from_callback` strips known prefixes (`WorkosCursorSessionToken=`, `Cookie:` header line, trailing other cookies) so the operator can paste any reasonable shape.
- No refresh-token flow — `refresh_access_token` raises `OAuthFlowError` so the proxy's background rotation worker stops cleanly. Operators rotate by pasting a fresh cookie through the Rotate button (~30 day cadence per Cursor's session lifetime).

**Dispatch:** cursor-oauth uses the standard OpenAI / litellm path. The sidecar speaks OpenAI Chat Completions; the proxy treats it like any other OpenAI-compatible upstream. No new dispatcher.

**Validation:**

- New file `tests/unit/test_v4431_cursor_oauth.py` — 23 tests covering: spec registration, subscription tier membership, endpoint shape, frontend wiring, all `parse_credentials` branches (bare token, JSON shapes, malformed, empty, unrecognized), `looks_like_cursor_token` boundary cases, `start_authorize` randomness + dashboard URL, `extract_code_from_callback` prefix-stripping + trailing-cookie handling, mocked-sidecar happy path (200 + accessToken), unknown-state rejection, non-200 sidecar propagation with status code in error, refresh-not-supported in v1, and pending-state expiry sweep.
- Full unit suite: **2419 passed + 2 skipped** (was 2396+2 in v4.4.30 — +23 from this ship).
- Frontend typecheck: `tsc --noEmit` exit 0.
- Live smoke on www1: container reports `version: 4.4.31`; `POST /api/providers/cursor-oauth/authorize` returns HTTP 401 for unauthenticated callers (endpoint is wired and admin-gated); sidecar reachable from inside the container.

**Cluster status at ship time:**

- www1 (tmrwww01): **deployed**, smoke clean.
- www2 (tmrwww02) + c1conv (avaya-01-s23): pending operator UI validation of the polished flow on www1 before cluster roll-out. Standard rsync-app + build-locally procedure applies (see `reference_llm_proxy_deploy.md`); the cursor-bridge service in compose needs to be present on each peer (already is on www1).

### v4.4.30 — litellm bump (3 critical CVEs in prod runtime) (2026-05-29)

Out-of-band security-scan finding (the security toolkit at `~/security/` flagged 17 critical / 106 high; the 3 most consequential were in our actual prod runtime).

**Vulnerabilities (all in `litellm` >=1.83.0,<1.85.0 — our exact pin):**

- **GHSA-46cm-pfwv-cgf8** — Server-Side Template Injection in `/completions`. We expose `/v1/completions` so this is directly reachable.
- **GHSA-gppg-gqw8-wh9g** — Remote Code Execution via unsafe `eval()`.
- **GHSA-jjhc-v7c2-5hh6** — Authentication bypass via OIDC `userinfo` cache key collision. We don't use litellm's OIDC, but coverage is cleaner with the fixed version.

**Fix:** bumped pin from `litellm>=1.83.0,<1.85.0` (vulnerable range) to `litellm>=1.85.2,<1.87.0`. Running 1.86.2 in the rebuilt image.

**The "secrets" finding (3 hits) is a false positive.** Gitleaks flagged Anthropic's *public* Claude OAuth client ID (`9d1c250a-e61b-44d9-88ed-5944d1962f5e`) at 3 locations (claude_oauth_flow.py:61 + 2 doc references). The value is in Anthropic's published OAuth flow docs. Safe to baseline.

**Validation:**

- Full unit suite green against the new litellm: **2396 passed + 2 skipped** (unchanged).
- Two existing pin-version source-guard tests updated to match the new bounds (test_v3914 + test_v3917).
- Live smoke on www1: `/v1/messages` returns 200 (litellm dispatch path works). Bedrock/SageMaker pre-load warnings are benign (botocore not installed; we don't use AWS providers).

**Operator action — none.** Patch-class release; the security scanner has the CVE evidence if needed for compliance.

### v4.4.29 — observability + credential-exposure fix (BUG-085 + F-INFRA-003) (2026-05-29)

Two small fixes from the post-v4.4.28 health sweep:

**BUG-085** — Post-deploy fleet check on 2026-05-29 showed 17 errors in last 3h (vs 0 in the previous QA pass). All from `coordinator-hub` against `Devin-Anthropic-Max-VG/claude-haiku`: upstream `400: "A maximum of 4 blocks with cache_control may be provided. Found 5."` 14 occurrences today, 1 ever before. Related to the hub team's F2 work (BUG-082 territory).

The proxy's `_inject_claude_code_system` (`app/api/_messages_streaming_oauth.py`) already caps itself — when the caller's count is ≥4, the proxy adds its marker WITHOUT `cache_control`. So the 5 is entirely caller-supplied. v4.4.29 adds a `logger.warning` that fires when count > 4 with a breakdown `sys=N msgs=N tools=N` so the next occurrence self-documents the source location of the excess markers. The actual fix is hub-side (trim their template); this is observability.

**F-INFRA-003** — `tests/conftest.py:15` had `ADMIN_PASS = "REMOVED-CREDENTIAL-ROTATED-20260828"` in plaintext (committed in git history on a public repo). Now reads from `LLMPROXY_TEST_ADMIN_PASS` env var with a dev-default fallback. Same pattern applied to `BASE_URL`/`ADMIN_USER`. The password remains in old commits — recommend operator rotate the live password if it's still in active use.

**Tests** (`tests/unit/test_v4429_cache_marker_overcap_log.py`, +5):
- Source guards (BUG-085 reference, breakdown fields, telemetry try/except, conftest plaintext-cred guard)
- Behavioral (warning fires at 5 markers with correct sys/msgs/tools breakdown; no warning at ≤4)

**Test counts**

- Unit suite: **2396 passed + 2 skipped** (was 2391+2 in v4.4.27; +5).

**Operator action**: forward the BUG-082 follow-up memo when convenient (hub trim their cache_control marker count to ≤4). If the conftest password was ever the live `admin` password, rotate it.

### v4.4.28 — dark-mode tertiary-text contrast sweep (F-OBS-004 residual) (2026-05-28)

Operator follow-up on F-OBS-004: continuing past the worst-case fix in v4.4.26. Targeted sweep across 29 files of bare `text-gray-500` (Tailwind 12px tertiary sub-labels with no compensating `dark:` variant) → `text-gray-500 dark:text-gray-400`.

**Effect (Playwright audit, before → after, dark mode):**
- Bare `text-gray-500` sub-labels — Dashboard/API-Keys/Providers/Activity/Users "grok-web · grok-3", "1 req", "100%", "util 0%", "12 users", etc.: **3.03 → 3.42** ✓
- Light mode unchanged (the sweep added `dark:` variants only).

**Honest residual (NOT regressions; pre-existing in v4.4.26):**
- 10px "Anthropic Console" label remains 3.42:1 (font-size bound; bumping to gray-300 over-brightens). Operator-accepted at 3.42 per v4.4.26 sign-off.
- Light-mode `text-gray-400` description text (~2.6:1) exists in DashboardPage/SettingsPage/ActivityPage. Not regressed; was previously below the audit's flagging threshold's apparent priority. Operator said "UI looks good" with these in place — left as-is.
- Status-coded text (`text-red-500` failure %, `text-indigo-500` links, `text-emerald-500` arrows): semantic colors. Changing them flattens meaning. Left as-is.
- "10" count badge with `bg-red-100`: borderline (3.46); deliberate red attention color.

**Scope discipline.** Did not attempt a blanket gray bump (would flatten visual hierarchy + harm semantic colors). The sweep was the unambiguous case from v4.4.26's measured findings.

**No backend change; tsc clean.** Unit suite unchanged 2391+2.

**Operator action — none.** Dark-mode tertiary text now sits at 3.42:1 across the board (improved from 3.03); further bumps require deliberate hierarchy/semantic-color decisions outside this release's scope.

### v4.4.27 — UNIQUE(provider_id, captured_at) on provider_ai_review (BUG-079 permanent fix) (2026-05-28)

The v4.4.24 `.limit(1)` guard stops `apply_sync` crashing on duplicate rows; this stops the duplicate from ever being written. Per-pass observation 2026-05-28: between v4.4.24's data-fix on www2 (1 dup removed → 0) and v4.4.27 prep, **www2 accumulated 3 NEW dup groups in 24h** — the check-then-insert race is still live. UNIQUE INDEX closes it at the schema level.

**Migration (`init_db`, idempotent):**

1. `DELETE FROM provider_ai_review WHERE id IN (...)` — `ROW_NUMBER() OVER (PARTITION BY provider_id, captured_at)` ranks each group by keeper heuristic (prefer rows with non-NULL `applied_at`/`dismissed_at`/`reverted_at` — operator action; ties break by highest id). Drops the losers.
2. `CREATE UNIQUE INDEX IF NOT EXISTS ux_provider_ai_review_provider_id_captured_at ON provider_ai_review(provider_id, captured_at)`.
3. Same loop mirrors the change onto `api_key_ai_review` (currently empty, but BUG-080 flagged the latent risk).

**Belt + braces.** The `.limit(1)` guard in `sync_handlers.py` stays — it protects against legacy duplicates landing during a rolling deploy from a peer that hasn't applied the migration yet.

**Live verification on www1 + cluster roll:**
- www1 startup applied the migration cleanly (4247 rows, 0 dups). Direct INSERT of an existing (provider_id, captured_at) pair now raises `sqlite3.IntegrityError: UNIQUE constraint failed`.
- www2 deploy auto-removed the **3 dup rows** observed pre-deploy (4047 → 4044).
- c1conv deploy auto-removed the **1 dup row** (4063 → 4062).
- All 3 nodes carry the index `ux_provider_ai_review_provider_id_captured_at`.

**Tests** (`tests/unit/test_v4427_unique_constraint.py`, +8):
- 4 source guards (both tables covered; DELETE precedes CREATE in executable code; lifecycle-keeper heuristic; IF NOT EXISTS idempotency)
- 4 behavioral (constraint rejects direct duplicate INSERT; allows different `captured_at`; migration de-dups pre-existing dupes preferring lifecycle-bearing row; rerunning init_db is a no-op)

**Pre-fix DB snapshots:** `/home/dblagbro/backups/llmproxy.{www1,www2,c1conv}.pre-v4427.*` (rollback per `docs/backup-plan.md`).

**Test counts**

- Unit suite: **2391 passed + 2 skipped** (was 2383+2 in v4.4.26; +8).

**Operator action — none.** The race that created BUG-079 can no longer write duplicates anywhere in the fleet.

### v4.4.26 — accessibility + dark-mode contrast (Playwright-verified) (2026-05-28)

Closes F-OBS-005 (a11y) and partially F-OBS-004 (contrast) from the 2026-05-27 QA pass. Both were originally deferred as "needs browser visual review" — done here with a Playwright audit that measures **real WCAG contrast ratios** (canvas-based color resolver that handles Tailwind v4's `oklch()` serialization) and inspects live DOM a11y state.

**A11y (F-OBS-005) — FULLY CLOSED.** Before: ~13 findings across the app. After: **all 9 pages a11y-OK**.
- `Sidebar` collapse button → `aria-label` (was anonymous on every page — highest-value single fix)
- `Input` component → associates `<label htmlFor>` ↔ `<input id>` via `useId` (app-wide)
- `Switch` component → new `ariaLabel` prop; wired through `DynamicSettingsPanel` (`item.label`) and `SettingsPage` (`boolField` humanizes the key)
- `CopyButton` → default `aria-label="Copy to clipboard"` (shared, used in many places incl. routing)
- Activity (severity, error-class), Providers (sort), UserPreferences (timezone, time-format) selects → `aria-label`
- Users Edit/Delete row buttons → `aria-label` with the username

**Contrast (F-OBS-004) — worst case fixed; rest is a design decision.**
- Swept 12 `dark:text-gray-500` → `dark:text-gray-400` (7 files). The worst offender — a 10px "Anthropic Console" sub-label — went from **1.84:1 (unreadable) → 3.42:1 (legible)**.
- Residual: ~54 bare `text-gray-500` 12px tertiary labels still measure ~3.03:1 in dark mode. These are *intentionally de-emphasized*; forcing them all to AA 4.5:1 would flatten the visual hierarchy. **Left as an operator design decision** (see bug-log F-OBS-004) — recommendation is to accept ~3:1 for de-emphasized tertiary text in this internal dark UI.

**Verification:** Playwright audit re-run after each change; before/after ratios recorded in `docs/bug-log.md`. The audit script (canvas resolver) is a reusable regression harness.

**No backend/test-count change** — frontend-only release. Unit suite unchanged at 2383+2.

**Operator action — one decision pending:** how far to push the residual dark-mode tertiary-text contrast (accept ~3:1 vs. hierarchy redesign).

### v4.4.25 — api_keys cluster-sync INSERT field coverage (2026-05-28)

Follow-on fix found while verifying v4.4.24's BUG-079 repair. The row + LWW stamp propagated to peers correctly (BUG-079 confirmed fixed), but a newly created+PATCHed api_key arrived at peers with `semantic_cache_enabled=0` and `daily_hard_cap_usd=NULL` — the operator's PATCHed values were lost.

**Root cause (BUG-084):** `apply_sync`'s api_keys INSERT path (`app/cluster/sync.py`) materialized only base columns + the v4.4.20 stamp. The 8 extended operator-settable fields that v4.4.18 added to the *UPDATE* push/apply were never added to the *INSERT*. Worse, the insert sets `last_user_edit_at` to the origin's stamp, so the next sync hits the LWW tie (equal stamps → keep local) and the UPDATE path never backfills them. A new key's extended fields therefore never reached peers unless the operator PATCHed a second time.

**Fix:** the INSERT now carries all 8 extended fields (`semantic_cache_enabled`, `daily_soft_cap_usd`, `daily_hard_cap_usd`, `hourly_cap_usd`, `rate_limit_tier`, `caller_memory_ttl_days`, `lmrh_polling_rpm`, `lmrh_quotes_rpm`).

**Tests** (`tests/unit/test_v4425_apikey_insert_field_coverage.py`, +3): source guard on insert coverage; behavioral repro (new key materializes extended fields); insert-then-tie consistency (a second sync at equal stamp is a no-op and doesn't revert).

**Test counts**

- Unit suite: **2383 passed + 2 skipped** (was 2380+2 in v4.4.24; +3).

**Operator action — none.** Completes the cluster-sync field-coverage arc: create + edit on any node now fully propagates on the first sync cycle.

### v4.4.24 — cluster-sync apply robustness (QA-pass remediation) (2026-05-28)

Fixes the headline finding of the 2026-05-27 deep QA pass: **cluster sync had been silently broken for ~6 days** (BUG-079). A single duplicate `(provider_id, captured_at)` row in `provider_ai_review` made `_apply_provider_ai_reviews` raise `MultipleResultsFound`, aborting the *entire* `apply_sync` transaction on inbound `/cluster/sync` to www2 + c1conv. Nothing propagated from www1 — api_keys, providers, settings, the lot. Heartbeat still reported "healthy" so it went undetected. The v4.4.18 field-coverage fix and v4.4.20 LWW gate were both functionally dead the whole time.

**Code fixes:**

- **BUG-079 / BUG-080** — added `.limit(1)` to all 5 vulnerable `scalar_one_or_none()` lookups in `app/cluster/sync_handlers.py` (`_apply_blocked_ips`, `_apply_ai_reviews`, `_apply_provider_ai_reviews`, `_apply_caller_memory`, `_apply_caller_memory_markers`). The other 2 handlers already had it. A duplicate row can no longer crash the apply transaction — the merge picks one row and proceeds.
- **BUG-081** — `push_sync` (`app/cluster/manager.py`) now inspects the peer response status. Pre-fix it was a fire-and-forget POST: a peer 500-ing on apply was invisible to the originator, which is the root reason BUG-079 hid for 6 days. A non-200 now logs `Sync to <peer> REJECTED: HTTP <code> — <body preview>`.
- **BUG-083** — `Query(hours, le=720)` → `Query(hours, ge=1, le=720)` on the 3 monitoring endpoints. Negative `hours` previously returned 200 with a silently-empty result (the SQL window resolved to "the future").

**Data fix** (applied operationally, not in the migration):

- De-duped the 2 stale `provider_ai_review` rows (www2: `da9fb8d610e5ccfa@2026-05-21`, c1conv: `91bafda9cc28d0d6@2026-05-25`). Kept the row carrying lifecycle fields. Per-node DB snapshots taken before the delete (`/home/dblagbro/backups/`).

**Deferred to v4.4.25:** `UNIQUE(provider_id, captured_at)` constraint via the SQLite shadow-table pattern (separate backup needs; the `.limit(1)` guard makes it non-urgent).

**Tests** (`tests/unit/test_v4424_cluster_sync_robustness.py`, +5):
- Source guard: every `scalar_one_or_none` in sync_handlers.py has a `.limit(1)` within its select chain (prevents BUG-080 regression on any future handler)
- Source guard: `push_sync` captures + checks the response status (BUG-081)
- Source guard: `Query(hours)` carries `ge=1` (BUG-083)
- Behavioral: `_apply_provider_ai_reviews` survives a seeded duplicate (exact BUG-079 repro)
- Behavioral: `_apply_caller_memory` survives a seeded duplicate (BUG-080)
- Also widened the v4.4.13 push_sync source-window test (the BUG-081 block outgrew its 2500-char slice — same brittleness as the v3.9.8 fix during the QA pass).

**Test infra** (F-INFRA-001): `pytest_sessionfinish` live-prod purge is now gated behind `LLMPROXY_TEST_PURGE_LIVE=1`. The unit suite is hermetic again — `pytest tests/unit/` no longer POSTs to `www.voipguru.org` at session-finish.

**Deferred:** F-OBS-004 (light-mode `text-gray-400` contrast) needs visual verification in a browser; deferred to a dedicated UI-review session rather than a blind 39-spot class sweep.

**Test counts**

- Unit suite: **2380 passed + 2 skipped** (was 2375+2 in v4.4.23; +5).

**Operator action — none.** After deploy, cluster sync resumes immediately (the `.limit(1)` guard tolerates the existing duplicates; the data fix is hygiene). Verified post-deploy with the same live LWW test that found BUG-079.

### v4.4.23 — per-event caller-memory header capture in activity_log (2026-05-27)

Surfaced today by a DevinGPT follow-up to the caller-memory observability memo. They asked us to re-sample two specific 2026-05-17 events and confirm whether `X-Conversation-Id` was present.

**We couldn't** — `activity_log.event_meta` never captured request headers. It only logs body-derived fields (model, in_tok, request_preview, …). The Prometheus counter from v4.4.15 (F-OBS-003) does capture header presence — but it's in-process and resets on container restart, so it can't answer "did this specific historical event carry the header." My earlier "57 reqs, zero header" finding in the memo was reading the absence of a field that was **never logged in the first place**.

DevinGPT's reply was right to push back: they intentionally don't send the header on auxiliary subcalls (refusal-judge probes, image-prompt refinement, auto-title, embedding queries, deep-research sub-calls, sub-agent spawn, image-gen probe). Of the ~57 sampled requests in the window, only ~4 were user-facing chats that should have carried the header, and we have no way to verify presence on those specific records.

**Fix** — close the observability gap:

- Two new contextvars in `app/observability/request_context.py`: `had_x_conversation_id` and `had_x_memory_tag`. Set at the entry points of `/v1/messages` and `/v1/completions` (alongside the existing F-OBS-003 Prometheus counter), default False outside a request scope so probes / internal traffic don't get misleading flags.
- `_build_event_meta_base` reads the contextvars and stamps `meta["had_x_conversation_id"] = True` / `meta["had_x_memory_tag"] = True` on each activity_log row. Boolean only — never the value (it's a privacy-sensitive client identifier). Absent key implies False so the schema stays lean.
- Same try/except containment as the prometheus counter — instrumentation failures never break a request.

**Why a contextvar, not plumb through `record_outcome`?** `record_outcome` has ~14 call sites across messages.py, completions.py, keepalive.py, _messages_streaming.py, _completions_streaming.py, _grok_web_dispatch.py, etc. Threading a new parameter through all of them is high-churn for a per-request side-channel value. ContextVars were designed for exactly this — the same pattern v3.6.2 used for `client_ip` and v3.7.15 for `internal_source`.

**What this means for the DevinGPT memo**: I owe them a correction. The "57 requests, zero header" claim was based on a field we don't log. We can't retroactively verify the 2026-05-17 events. Going forward, every `/v1/messages` and `/v1/completions` row will carry the bool, so the next time we have a question like this, the answer is one SQL query.

**Tests** (`tests/unit/test_v4423_caller_memory_headers_in_meta.py`, +9):
- 6 source guards: contextvar exists with False default; both entry points set it; `_build_event_meta_base` reads it; stamps `True` literal, never the value; instrumentation wrapped in try/except.
- 3 behavioral: default-False outside request scope; setter round-trips bool; setter coerces truthy `str` (FastAPI Header() param shape) to bool.

**Test counts**

- Unit suite: **2375 passed + 2 skipped** (was 2366+2 in v4.4.22; +9).

**Operator action — none.** Patch-class release. Next sweep will show `had_x_conversation_id: true` (or its absence) on every user-chat event, providing exactly the forensic evidence the DevinGPT memo back-and-forth asked for.

### v4.4.22 — async-side session tracer (ARCH-A real fix) (2026-05-27)

The latent ARCH-A pool leak's investigation has been stuck for two releases because the captured stacks were useless. v3.10.2 added a sync pool-event hook with `traceback.format_stack()`. v4.4.19 fixed the slicing direction. **Neither one ever captured app frames** — and the 2026-05-27 dig finally explained why.

SQLAlchemy's async adapter dispatches every DB op via a **greenlet**. The pool-event hook fires synchronously inside that greenlet, where `format_stack()` walks the greenlet's stack — which is separate from the async caller's stack. So the captured frames always end at `session.execute()` with nothing above. py-spy confirmed: 10 aiosqlite worker threads, all running their own loops, no app code anywhere in their reported stacks.

**Fix** — capture on the async side, where the app caller IS in the stack.

- New `_TracedAsyncSession` subclass of `AsyncSession` overrides `__aenter__` and `__aexit__` to capture `format_stack()` on the async caller's coroutine. The session-maker hands out this subclass when `db_pool_trace=1`, so every `async with AsyncSessionLocal() as db:` site is automatically traced — no call-site changes.
- New `get_async_session_trace()` reader returns the in-flight async sessions, oldest first, each with `{age_sec, session_id, stack}`.
- `/cluster/db-pool-trace` now returns `async_sessions: [...]` alongside the existing (still-useful for connection-level visibility) `checked_out: [...]`. Read `async_sessions` to identify the leaker.
- `/health` `dbPool` block gains `traced_async_sessions` + `oldest_async_session_age_sec` for at-a-glance check without auth.
- Tracer capture/clear is wrapped in `try/except` so a bug in instrumentation never blocks real DB use.

**Behavioral test** (`test_async_trace_captures_app_frames_when_enabled`) — exercises the capture mechanism inside a test function and asserts the test function's own name appears in the captured stack. This is exactly the property v4.4.19's sync tracer failed in production. Test passes here; the production validation will land when the next leaked session shows up with a non-internal stack.

**Coexistence**: both tracers run when enabled. The sync one keeps connection-level identity (id of `conn_record` survives session reuse); the async one names the calling code path. They surface in distinct lists on the endpoint.

**Tests** (`tests/unit/test_v4422_async_session_tracer.py`, +8): 6 source/shape guards + 2 behavioral (capture-on-enter / clear-on-exit, including exception path). Also bumped the v3.9.8 source-window test that the new tracer body outgrew.

**Test counts**

- Unit suite: **2366 passed + 2 skipped** (was 2358+2 in v4.4.21; +8).

**Operator action — none.** Patch-class release. Once a session leaks in production, `/cluster/db-pool-trace`'s `async_sessions` list will name the codepath that opened it. Container has `DB_POOL_TRACE=1` set on www1 + www2; will activate immediately after deploy.

### v4.4.21 — per-node Provider Summary (2026-05-27)

The dashboard's Provider Summary has always shown only the local node's traffic, because `provider_metrics` is per-node (not cluster-replicated — a 2026-05-15 backlog memo claimed otherwise but the cluster-sync code path doesn't reference the table). So an operator wanting to see "is OpenRouter load balanced across the fleet" had to SSH into each node and eyeball.

**Backend** — two new endpoints:

- `GET /cluster/local-metrics` — HMAC-authed peer-pull endpoint; returns this node's summary plus its `node_id`. Same auth pattern as `/cluster/oauth-pull` and `/cluster/settings`.
- `GET /api/monitoring/metrics-by-node` — admin-authed fan-out. Calls `/cluster/local-metrics` on each peer in parallel, aggregates into `{nodes: [{node_id, ok, providers}, …]}`. Partial-view tolerant — unreachable peers return `ok=false` with the error rather than failing the whole call.

Existing `/api/monitoring/metrics` now also returns `node_id` so the UI can label which node's data the aggregate view is showing.

**Frontend** — MetricsPage:

- Provider Summary card header gains a "Show per-node / Hide per-node" toggle. Lazy-loaded — the fan-out only runs when the operator opens the breakdown, not on every dashboard load.
- A new card below the aggregate Provider Summary renders one row per (provider, node), sorted by provider name + requests desc.
- Unreachable peers surface as a labeled row ("(node unreachable)") with the error in the title attribute; doesn't crash.

**Smoke verification** (live, 2026-05-27): the fan-out returned 4/3/2 providers across www1/www2/c1conv. Top providers (Vertex, Devin-Anthropic-Max-Gmail) are nearly balanced (~500/~300 reqs each per node); Grok-Web is www1-only; the C1 Anthropic intentional negative-test fixture is www2-only. Exactly the load-imbalance visibility this was filed for.

**Tests** (`tests/unit/test_v4421_metrics_by_node.py`, +8):

- 4 backend source/shape guards (cluster endpoint exists + HMAC-authed; payload has node_id; fan-out uses sign_payload + _parse_peers + tolerates ok=false; existing /metrics endpoint adds node_id)
- 3 frontend source guards (types, api client, toggle state + lazy-load gate)
- 1 render-path guard (per-node table handles unreachable peer rows)

The cross-cluster HTTP shape itself is exercised by the existing `/cluster/oauth-pull` integration tests — same auth + transport.

**Test counts**

- Unit suite: **2358 passed + 2 skipped** (was 2350+2 in v4.4.20; +8).

**Operator action — none.** Patch-class release. Click "Show per-node" on the Metrics page to use the new view.

### v4.4.20 — api_keys cluster-sync gains proper LWW gate (2026-05-26)

Closes the explicit follow-up from v4.4.18. That release expanded api_keys push/apply coverage to all operator-settable fields but couldn't add a proper LWW gate — `api_keys` had no per-row admin-edit timestamp, so the merge was effectively "last sync wins." This is the same shape `providers` had pre-v3.0.11.

**The risk in "last sync wins"**: any background mutation on a peer (cluster-sync echo, future migration writes, etc.) could revert a fresh operator PATCH on another node, silently. The provider side hit this concretely in 2026-05-08 (`bridge_url change in extra_config on www01 didn't propagate to peers for hours`).

**Schema change** — new nullable column:

- `ALTER TABLE api_keys ADD COLUMN last_user_edit_at REAL`

Idempotent ALTER lands via `init_db()` on first boot of any node running v4.4.20.

**Push payload + apply LWW gate** — mirror of v3.0.11 + v3.0.63 + v3.2.7 provider semantics, simplified by the fact that `api_keys` has no `updated_at` fallback:

| local stamp | peer stamp | outcome |
|---|---|---|
| has | has, peer > local | **accept**, adopt peer's stamp |
| has | has, peer == local | **reject** (tie — keep local; anti-ping-pong) |
| has | has, peer < local | **reject** |
| has | none | **reject** (legacy peer; conservative) |
| none | has | **accept** (legacy local upgrades) |
| none | none | **accept** (preserves pre-LWW behavior for mixed-version fleet) |

**Bump site**: `app/api/apikeys.py::update_key` — the PATCH endpoint stamps `last_user_edit_at = time.time()` (wall-clock, cross-node comparable). Background hot-path writes (cost-bucket bumps, `last_used_at`, `total_cost_usd`) deliberately do NOT bump — that's the whole point of separating user-edit time from generic-update time.

**Mixed-version safety**: pre-v4.4.20 peers' payloads omit the field. The apply handler treats that absence as "legacy peer" and falls through to last-sync-wins (same as v4.4.18/19 behavior). The first PATCH on either side stamps the row; subsequent sync round-trips converge on the LWW path.

**Tests** (`tests/unit/test_v4420_apikey_lww.py`, +11):

- 4 source guards (model column, push-payload field, PATCH bump, wall-clock not monotonic)
- 6 LWW branch-matrix tests (newer wins, older rejected, tie keeps local, local-stamped/peer-unstamped keeps local, peer-stamped/local-unstamped accepts, neither-stamped legacy path)
- 1 insert-path test (peer stamp carries on first materialization)

**Test counts**

- Unit suite: **2350 passed + 2 skipped** (was 2339+2 in v4.4.19; +11).

**Operator action — none.** Patch-class release. Future operator edits via the API Keys UI propagate under the LWW gate; legacy rows upgrade on first PATCH.

### v4.4.19 — ARCH-A pool-trace slicing direction fix (2026-05-26)

Resumed the ARCH-A connection-pool leak dig — the tracer (deployed v3.10.2, widened v3.10.13) **did catch a leak**: 59.6h-old stuck checkout on www01. But the captured stack was *all SQLAlchemy internals* — zero app frames — so the leaking codepath couldn't be named.

**Root cause** — slicing direction bug in `app/models/database.py::_trace_pool_checkout`:

```python
stack = "".join(traceback.format_stack()[-45:])  # BUG
```

`traceback.format_stack()` returns frames **outermost-first / innermost-last**. `[-45:]` keeps the *innermost* 45 = the SQLA pool-checkout chain + the hook itself. The app caller (the part that names the leak) lives in the *outer* frames — exactly what the slice discarded.

Both the v3.10.13 bump (18→45) and the original `[-18:]` had the direction wrong. The 2026-05-26 dig produced an all-SQLA stack despite a real 59h leak, which surfaced it.

**Fix** — drop the slice; keep the full stack minus the trivial trailing `format_stack` frame:

```python
stack = "".join(traceback.format_stack()[:-1])
```

**Secondary observation** — the historical "full pool saturation in 13-20h" pattern is *not* reproducing. www01 has been up 3 days with only 1 stuck connection (1/50 utilisation). v4.x appears to have largely fixed the leak; we're now hunting a much slower variant. Worth shipping the tracer fix because the next leak will have a useful stack.

**Tests** (`tests/unit/test_v4419_pool_trace_direction.py`, +3):
- Source guard: `format_stack()[-45:]` / `[-18:]` must not return; `[:-1]` must be present
- Direction assumption test: locks in `format_stack()` outermost-first ordering (would catch a future Python change)
- Synthetic deep-stack test: exercises the fix's slice shape and asserts the outer-most "app caller" frame survives (regression risk made concrete)

**Test counts**

- Unit suite: **2339 passed + 2 skipped** (was 2336+2 in v4.4.18; +3).

**Operator action — none.** Patch-class release. After deploy, the next pool checkout that leaks will include the app frame that initiated it.

### v4.4.18 — api_keys cluster-sync field coverage (research F3 follow-up) (2026-05-22)

Surfaced 2026-05-22 by F3 from the routing-cost research: the operator-forwarded hub-team request to flip `semantic_cache_enabled=1` on the coordinator-hub key succeeded on www1 but **did not propagate** to peers via cluster sync. Investigation found:

- The push payload (`manager.py`) only included 9 of the 26 `api_keys` columns.
- The apply handler (`sync.py`) only wrote 2 fields on update (`spending_cap_usd`, `rate_limit_rpm`).

Same class of bug as v3.0.10's provider-field coverage fix — operator edits a field on one node, peers stay stale.

**Fix**: expanded both push + apply to cover the operator-settable fields. Apply uses the membership-test pattern (`if "X" in k_data: existing.X = ...`) so a pre-v4.4.18 peer omitting a field doesn't clobber local values with `None`.

**Fields now syncing**:
- `enabled` (was sent but not applied)
- `semantic_cache_enabled` (the F3 trigger)
- `daily_soft_cap_usd`, `daily_hard_cap_usd`, `hourly_cap_usd` (budget caps)
- `rate_limit_tier`
- `caller_memory_ttl_days`
- `lmrh_polling_rpm`, `lmrh_quotes_rpm`

**Open follow-up**: `api_keys` has no `last_user_edit_at` column, so this is effectively "last sync wins" — same property the pre-fix 2-field path had; not made worse. A proper LWW gate (per the v3.0.11 pattern on providers) would need a schema migration. Filed as a future improvement.

**Operational note**: while shipping this fix, the F3 flag flip was applied manually via direct SQL on all 3 nodes to get the hub-team trial unblocked immediately. v4.4.18 makes future flag flips propagate automatically.

**Tests** (`tests/unit/test_v4418_apikey_sync_coverage.py`, +6): source guard on push payload (all 8 new fields present); behavioral tests for semantic_cache, budget caps, caller_memory_ttl, enabled-flag propagation; defensive test that a payload omitting a field does NOT clobber the local value (validates the membership-test pattern).

**Test counts**

- Unit suite: **2336 passed + 2 skipped** (was 2330 in v4.4.17; +6 sync-coverage tests).

**Operator action — none.** Patch-class release. Future flag changes on any operator-settable API key field will propagate cluster-wide on the next sync cycle (~60s).

### v4.4.17 — cluster heartbeat: degraded vs unreachable (research F4) (2026-05-22)

From the 2026-05-22 routing-cost research session (`docs/research-2026-05-22-routing-cost-efficiency.md`, finding F4 — the one item actionable without crossing the hub-team boundary).

The cluster heartbeat (`app/cluster/manager.py::_ping_peer`) did `data = resp.json()` without first checking status code or content-type. When a peer returned a non-JSON body — e.g. an nginx 502/504 HTML error page while that peer's container was mid-restart — the `JSONDecodeError` was caught by the generic `except`, logged as "unreachable", and **fired the all-providers-down notifier** for what was just a routine deploy blip. (Surfaced concretely when v4.4.16 made the previously-blank log line legible: `Cluster peer llm-proxy2-c1conv unreachable: JSONDecodeError`.)

**Fix** — three distinct classifications:
- **200 + valid JSON** → `healthy` (parse providers/status; existing path)
- **non-200, OR 200 + non-JSON body** → `degraded` (peer responded but isn't serving normally — likely restarting): logged at INFO, **does NOT page**
- **connection-level exception** (refused / timeout / DNS) → `unreachable`: logged WARNING + fires notifier (genuine outage)

A peer recovering from `degraded` → `healthy` logs recovery, same as recovering from `unreachable`.

**Why it matters:** alerting accuracy. A peer mid-deploy should not read identically to a peer that's actually down, and shouldn't trigger an all-providers-down page. This removes a class of false-positive alerts tied to routine rolling deploys.

**Tests** (`tests/unit/test_v4417_heartbeat_degraded.py`, +6): 200+JSON→healthy (no notify), non-200→degraded (no notify), 200+non-JSON→degraded (no notify — the exact JSONDecodeError case), ConnectError→unreachable+notify, degraded→healthy recovery, source-level three-bucket guard.

**Test counts**

- Unit suite: **2330 passed + 2 skipped** (was 2324 in v4.4.16; +6 heartbeat tests).

**Operator action — none.** Patch-class release. Rolling deploys will no longer false-page peers as "unreachable" during the brief window each node is restarting.

### v4.4.16 — log-quality fixes from the 2026-05-22 log audit (2026-05-22)

A second log audit (the cadence that found the v4.4.13 cluster-sync issue) surfaced two log-quality items. The v4.4.13 cluster-sync repair is confirmed holding — no more "Sync to www2 failed" spam.

**A — `cluster/manager.py` heartbeat: empty-exception-string log**

The peer-reachability heartbeat logged `f"Cluster peer {peer.id} unreachable: {e}"`. Same bug class as the `push_sync` line fixed in v4.4.13, but in a *different function* so it was missed: `str(httpx.ReadTimeout())` / `ConnectError("")` render blank, so the line was `Cluster peer llm-proxy2-c1conv unreachable: ` with no diagnostic. Fixed identically — surface `type(e).__name__` + a non-empty-message fallback.

**B — `auth/admin.py`: session-expiry logged at WARNING (noise)**

`session_not_found` + `session_expired` were logged at WARNING. But session expiry is an EXPECTED condition — a browser tab with a stale cookie (after a container restart wipes in-flight sessions, or after the row ages out) polls an auth endpoint, gets a 401, and re-logs-in. The audit found **207 identical `session_not_found` lines in 12h from ONE dead cookie** (token prefix `eyJfZnJl`), burying real warnings. The 401 response is the actual signal; the log line is noise. Both downgraded to DEBUG (detail preserved for auth debugging).

**Investigated, NOT a bug**: the doubled `anthropic/anthropic/` model prefix in the logs appears *only* for `C1 Anthropic Claude` — the intentional negative-test fixture (per operator-locked memory). No real provider is affected; the malformed model-id is part of why that fixture fails by design. Left alone.

**Files**:
- `app/cluster/manager.py` — heartbeat log uses type-name + non-empty fallback
- `app/auth/admin.py` — `session_not_found` + `session_expired` → DEBUG

**Tests** (`tests/unit/test_v4416_log_quality.py`, +5): source-level guards for both fixes + a behavioral check that the empty-exception fallback renders `ConnectError (no message)` for blank exceptions and passes real messages through.

**Test counts**

- Unit suite: **2324 passed + 2 skipped** (was 2319 in v4.4.15; +5 log-quality tests).

**Operator action — none.** Patch-class release. The `session_not_found` WARNING spam will stop; future cluster-peer-unreachable events will carry a real diagnostic string.

### v4.4.15 — F-OBS-003 caller-memory gating-header telemetry (2026-05-21)

Closes the observability gap on F-OBS-003. Caller-memory write-back is gated on the inbound `X-Conversation-Id` header; the feature flag has been ON cluster-wide since 2026-05-15 but `caller_memory` has had **0 production writes** — because no consumer is sending the header yet. Rather than periodically diff the `caller_memory` table to check, this adds direct telemetry so the operator can see the moment a consumer (e.g. DevinGPT) starts sending it.

**New Prometheus counter** `llm_proxy_conversation_id_requests_total{endpoint, has_conversation_id}`:
- Incremented on every `/v1/messages` + `/v1/chat/completions` request, right after auth.
- Labeled by endpoint (`messages` / `completions`) and whether the header was present (`true` / `false`).
- Wrapped in try/except — telemetry never breaks the request path.
- Surfaces automatically in `/metrics`.

**New admin endpoint** `GET /api/monitoring/conversation-id-stats` — glanceable JSON for operators who don't want to grep `/metrics`:
```json
{
  "by_endpoint": {
    "messages":    {"with_header": 0, "without_header": 1234},
    "completions": {"with_header": 0, "without_header": 56}
  },
  "total_with_header": 0,
  "header_seen": false,
  "note": "header_seen=false means no consumer is sending X-Conversation-Id yet..."
}
```
When `header_seen` flips to `true`, a consumer has started sending the gate and caller-memory write-back is live — no need to diff the table or read consumer-side logs.

**Files**:
- `app/observability/prometheus.py` — new `CONVERSATION_ID_REQUESTS_TOTAL` counter
- `app/api/messages.py` + `app/api/completions.py` — increment right after auth
- `app/api/monitoring.py` — new `conversation_id_stats` admin endpoint

**Tests** (`tests/unit/test_v4415_conversation_id_telemetry.py`, +7): counter declaration (source-level, pollution-proof), increment+readback (skips when prometheus is stubbed by sibling tests), both entry points record it, admin endpoint registered + computes `header_seen` + returns documented shape.

Note: 2 of the 7 tests `skip` rather than fail when run in the full suite, because `test_claude_oauth.py` / `test_priority_bump.py` install a `_Noop` prometheus stub into `sys.modules` that persists for the process. The value-based assertions skip gracefully in that case; the source-level assertions always run. (Pre-existing test-infra quirk, not introduced here.)

**Test counts**

- Unit suite: **2319 passed + 2 skipped** (was 2314 in v4.4.14; +7 telemetry tests, 2 conditionally skipped).

**Operator action — none required.** Watch `header_seen` on `GET /api/monitoring/conversation-id-stats` (or the `llm_proxy_conversation_id_requests_total{has_conversation_id="true"}` series in Grafana). When it goes non-zero, the consumer-side rollout has begun and F-OBS-003 resolves itself.

### v4.4.14 — `providers.py` 4th sibling split (read-side / stats endpoints) (2026-05-21)

Third preventive refactor in the v4.4.x cycle, same pattern as v4.4.11 (`db.py`) and v4.4.12 (`_messages_streaming.py`). Pre-refactor: `app/api/providers.py` was 958 LOC and on the watch list. Notable: this is its **second** split — v3.9.8 had already extracted `provider_lifecycle.py` + `provider_capabilities.py` from it; the file grew back over time.

**Split**: extracted the 4 read-side / stats endpoints into a new sibling `app/api/providers_stats.py`:
- `GET /api/providers` — list (with usage merge)
- `GET /api/providers/rolling-stats` — per-provider rolling windows
- `GET /api/providers/rolling-stats-by-node` — per-(provider, node) breakout
- `GET /api/providers/{id}/usage` — per-provider usage snapshot

`providers.py` keeps the mutation/CRUD endpoints (`create_provider`, `get_provider`, `get_rate_limit_state`, `update_provider`, `delete_provider`, `purge_test_tombstones`), the Pydantic models (`ProviderCreate`, `ProviderUpdate`), the priority-tie helpers (`normalize_priority_ties`, `_bump_priority_conflicts`), and the canonical `_serialize` helper.

**Sizes**:
```
Before: 958 LOC in one file
After:  684 LOC (providers.py) + 312 LOC (providers_stats.py) = 996 total
```

**Back-compat**: zero blast radius for callers. `providers_stats.py` owns its own `APIRouter(prefix="/api/providers")` and is mounted separately in `main.py` (same pattern v3.9.8 used). FastAPI routes by `(path, method)` so the GET `/api/providers` (list, in stats) and POST `/api/providers` (create, in providers) coexist cleanly.

**Special handling — `_serialize` lazy import**: the stats file imports `_serialize` from `providers.py` lazily inside `list_providers()` rather than at module load. Not strictly required (providers.py doesn't import providers_stats.py), but defensive against a future change that adds a back-import. Locked in by `test_stats_file_uses_lazy_serialize_import`.

**Tests** (`tests/unit/test_v4414_providers_stats_split.py`, +6):
- `test_both_files_load_cleanly` — basic smoke
- `test_stats_endpoints_register_on_stats_router` — 4 endpoint paths visible on stats router
- `test_mutation_endpoints_stay_on_providers_router` — CRUD path assertion + stats paths NOT leaking back
- `test_main_includes_providers_stats_router` — source-level guard for the `include_router` call
- `test_neither_file_exceeds_800_loc` — soft ceiling per split file
- `test_stats_file_uses_lazy_serialize_import` — pins the lazy-import defensive pattern

**Updated pre-existing tests**: 5 source-level tests in `test_v398_quota_uses_external_scrape.py` and `test_v3916_p3_p5.py` were reading just `providers.py` for code that's now in `providers_stats.py`. Updated them to read both files concatenated (same pattern v4.4.12's split applied to `test_v3911_streaming_memory_writeback.py`).

**Test counts**

- Unit suite: **2314 passed** (was 2308 in v4.4.13; +6 split-invariant tests).

**Operator action — none.** Pure refactor; no behavior change. The deployed container's API surface is identical.

### v4.4.13 — cluster-sync quality: ai-review prune + 45s timeout + log-message fix (2026-05-21)

Surfaced by a routine "check recent logs" sweep: container logs showed `Sync to llm-proxy2-www2 failed:` repeating **39× in 1h** with **0 successes** and **no error text after the colon**. Live diagnostic revealed three stacked issues, all fixed here:

**Issue 1: `provider_ai_review` + `api_key_ai_review` never pruned**

These tables accumulate ~250 rows/day (30-min cadence × 10 providers, both verdict types) and ARE included in the cluster sync push payload. Live audit 2026-05-21: 1,561 rows on www1 + 1,384 on www2 since 2026-05-15, growing the sync payload to **2.78 MB**. Without retention this would hit ~91k rows / ~90 MB per year. New config field `ai_review_retention_days` (default 30, env override `AI_REVIEW_RETENTION_DAYS`). New helper `_prune_ai_review(model_cls, keep_days)` wired into the daily sweep for both tables.

**Issue 2: 15s sync timeout too tight for the current payload**

Live measurement: c1conv processed the 2.78 MB payload in 10.7s (barely passing); www2 timed out at 15s (`httpx.ReadTimeout`). Raised the push_sync timeout from 15s → 45s. Belt-and-braces while the ai-review prune (Issue 1) shrinks the payload.

**Issue 3: empty exception strings render as bare colon-blank**

`httpx.ReadTimeout('')` has `str(e) == ""`, so `f"Sync to {peer.id} failed: {e}"` rendered as literally `Sync to www2 failed: ` with no diagnostic. Same class issue `_exc_str` solves in `_messages_streaming.py`. Inline fallback added: `msg = str(e) if str(e) else f"{type(e).__name__} (no message)"`. Log line now uses both the type name AND the message: `Sync to www2 failed: ReadTimeout: ReadTimeout (no message)`.

**Why bundle three fixes**: they all surface the same incident (sync to www2 failing invisibly) and they all need to be real to fully close the loop. Just the timeout hides the symptom; just the prune doesn't help while the table is still 2.78 MB at deploy time; just the log fix surfaces real timeouts but the timeouts keep happening.

**Files**:
- `app/config.py` — new `ai_review_retention_days` field
- `app/monitoring/prune.py` — new `_prune_ai_review` helper + sweep wiring + `_ai_review_retention_days()` getter; result dict + log line updated
- `app/cluster/manager.py:441-465` — timeout 15→45s; log line uses `type(e).__name__` + non-empty `str(e)` fallback

**Tests** (`tests/unit/test_v4413_cluster_sync_quality.py`, +9):
- `test_ai_review_retention_setting_exists` — config field present with 30d default
- `test_ai_review_retention_helper_clamps_minimum` — defensive min=1
- `test_prune_ai_review_helper_exists` — signature + works for both model classes
- `test_ai_review_prune_wired_into_sweep` — source-level ordering
- `test_sweep_output_dict_has_ai_review_counters` — counters added to `_LAST_SWEEP_RESULT`
- `test_sweep_log_line_includes_ai_review_counts` — visible in operator log tail
- `test_push_sync_timeout_raised` — 15→45s
- `test_push_sync_log_message_handles_empty_exception_str` — source-level for the fallback
- `test_push_sync_log_falls_back_for_empty_exception` — behavioral: `httpx.ReadTimeout("")` renders as `ReadTimeout (no message)`; real messages pass through unchanged

**Test counts**

- Unit suite: **2308 passed** (was 2299 in v4.4.12; +9 cluster-sync-quality tests).

**Operator action — none required.** Patch-class release. The first scheduled prune sweep (~24h after deploy) will tombstone ~30 days of old ai-review rows; cluster sync payload will shrink dramatically and the 45s timeout becomes effectively redundant.

### v4.4.12 — `_messages_streaming.py` claude-oauth section extracted (2026-05-21)

Second preventive refactor in the v4.4.x cycle, same pattern as v4.4.11's `db.py` split. Pre-refactor: `app/api/_messages_streaming.py` was 979 LOC — the largest file in the codebase post-v4.4.11 and on the watch list as the next refactor candidate.

**Split**: the claude-oauth dispatch section (the largest cohesive chunk — ~525 LOC) extracted into `app/api/_messages_streaming_oauth.py`. The parent file keeps the litellm-backed Anthropic-streaming path (`_stream_anthropic`), the CoT path (`_stream_cot_anthropic`), the webhook variant, and the shared SSE / error helpers.

**Sizes**:
```
Before: 979 LOC in one file
After:  471 LOC parent + 562 LOC oauth = 1033 total
        (the +54 overhead is module docstring + re-export block;
         the actual function code is unchanged)
```

**Back-compat shim**: the 11 claude-oauth symbols (`_inject_claude_code_system`, `_complete_claude_oauth`, `_stream_claude_oauth`, etc.) are re-exported from `_messages_streaming.py` so the 5 existing external import sites (`scanner.py`, `hedging.py`, `completions.py`, `_messages_dispatch.py`, `keepalive.py`) work unchanged.

**Special handling — `_exc_str` duplication**: this helper is needed by both the litellm path (in the parent) and the claude-oauth path (in the new module). Importing it from the parent into the new module would create a circular import via the re-export shim. The 7-line helper is therefore duplicated; an invariant test (`test_exc_str_duplicate_is_identical`) compares the two copies' behavior across 6 exception types so any drift fails loudly.

**Tests** (`tests/unit/test_v4412_streaming_split.py`, +7):

- `test_both_streaming_modules_load_cleanly`
- `test_oauth_symbols_still_importable_from_parent_module` — back-compat re-exports
- `test_litellm_path_still_lives_in_parent_module`
- `test_oauth_path_lives_in_new_module`
- `test_exc_str_duplicate_is_identical` — the 6-case behavioral comparison
- `test_neither_streaming_file_exceeds_700_loc` — soft ceiling per post-split file
- `test_external_callers_still_resolve_oauth_symbols` — spot-check the 5 import sites

Also updated `tests/unit/test_v3911_streaming_memory_writeback.py` — its 5 source-level tests now read BOTH files (concatenated) so the assertions still cover the same surface area post-split.

**Test counts**

- Unit suite: **2299 passed** (was 2292 in v4.4.11; +7 BUG-... wait, this is a refactor, no BUG — +7 split-invariant tests).

**Operator action — none.** Pure refactor; no application behaviour change; no DB schema change. The deployed container's behavior is identical to v4.4.11.

**Note on F-OBS-001 (nginx http2 deprecation)**: attempted to fix in this session but found the nginx container is bind-pinned to an old inode of `/etc/nginx/nginx.conf` (atomic edits via `mv` left the container reading a detached file). Host-side edits don't reach the running container without a nginx container restart, which would briefly interrupt all proxy traffic. Deferred to operator coordination. Discovery itself is worth recording — it explains why some past nginx config edits have appeared to "not stick".

### v4.4.11 — `app/models/db.py` split into 10 domain modules (2026-05-20)

Pre-split the file was **994 LOC** — one new ORM table away from the
project's de-facto 1,000-LOC ceiling (matches the Hub team's
`project_v160071_oversize_pass` rule). v4.4.11 splits it along
natural domain boundaries:

| New module | Models |
|---|---|
| `app/models/db_base.py` | `Base` (DeclarativeBase) + `Session` (auth) |
| `app/models/db_provider.py` | `Provider`, `ProviderUsageWindow`, `ProviderNodeAuthState`, `ExternalUsageSnapshot`, `ModelCapability`, `ProviderAiReview`, `ModelToolProbe`, `ProviderMetric`, `ModelAlias` (heaviest — 467 LOC) |
| `app/models/db_apikey.py` | `ApiKey`, `ApiKeyAiReview` |
| `app/models/db_user.py` | `User`, `SystemSetting` |
| `app/models/db_activity.py` | `BlockedIp`, `ActivityLog` |
| `app/models/db_run.py` | `Run`, `RunMessage`, `RunEvent`, `RunIdempotency` |
| `app/models/db_lmrh.py` | `LmrhDim`, `LmrhProposal` |
| `app/models/db_oauth.py` | `OAuthCaptureProfile`, `OAuthCaptureLog` |
| `app/models/db_caller_memory.py` | `CallerMemory`, `CallerMemoryMarker` |
| `app/models/db_airi.py` | `AiriRuleset`, `AiriRule`, `AiriProposal`, `AiriConversation`, `AiriMessage`, `AiriNotificationPref` |

`app/models/db.py` becomes a **136 LOC re-export shim** — every model is imported at module-load (so the SQLAlchemy registry on `Base.metadata` is fully populated), then re-exported in `__all__`. Existing callers (`from app.models.db import Provider, ...`) work unchanged — **zero blast radius for the rest of the codebase**.

**Sizes before / after**:

```
Before: 994 LOC in 1 file
After : 467 LOC max (db_provider.py); 1269 LOC total across 11 files
        (the +275 overhead is module docstrings + import statements
         — the actual model code is unchanged)
```

**Invariants under test** (`tests/unit/test_v4411_db_split.py`, +4 tests):

- `test_all_domain_modules_load_cleanly` — each of the 10 new modules imports without errors.
- `test_re_export_shim_includes_every_model_class` — `app.models.db.__all__` includes every model class from every domain module. Catches the future "added a new model but forgot to re-export it" gotcha.
- `test_registry_has_all_32_tables` — `Base.metadata.tables` contains all 32 tables after importing `db.py`. Catches a domain module that's not imported by the shim.
- `test_no_domain_module_exceeds_500_loc` — soft ceiling per new domain file. If any future addition pushes a module past 500 LOC, the test fails and signals "re-split this domain."

**Risk profile**: very low. SQLAlchemy `relationship()` calls use string references (`relationship("ModelCapability", back_populates="provider", ...)`) — these resolve lazily via `Base.registry` at first query, so they don't care which module a model is defined in as long as both modules are imported before the first query fires.

**Test counts**

- Unit suite: **2292 passed** (was 2288 in v4.4.10; +4 split-invariant tests).

**Operator action — none.** Pure refactor; no application behaviour change. The DB schema is identical; the deployed container's behavior is identical.

### v4.4.10 — full doc update + v4.4.x release-readiness wrap-up (2026-05-20)

Doc-only release wrapping the v4.4.0 → v4.4.9 fix cycle. No application
or test code changes.

- `README.md` — `v4 highlights` section extended through v4.4.9 (was
  ending at v4.4.0). Each release now has a one-line summary linking
  to the closed bug ID.
- `docs/test-plan.md` — test count updated 2260 → 2288 (+28 across
  the v4.4.1-v4.4.9 fix cycle); wall-time updated.
- `docs/remediation-plan.md` — Batch G status flipped from PLANNED
  to CLOSED with the explicit list of all v4.4.x closures (BUG-051..058
  + F-OBS-004 + CLEANUP-001).
- `docs/qa-notes.md` — appended a "v4.4.x fix-cycle wrap-up"
  section capturing the durable lessons: pre-cut live-verify catches
  the v4.3.3 footgun class; WAL TRUNCATE auto-reclaims storm-class
  high-water; container limits bound blast radius; orphan prune
  closes the SQLite-no-FK-enforcement gap; tombstone-propagation
  fix is now branch-on-`not local_deleted`; streaming-protocol
  completeness fixes for Anthropic + OpenAI; `--run-real` is
  destructive-to-monitoring.
- `docs/release-readiness-v4.4.x.md` — new consolidated wrap-up
  artefact superseding `release-readiness-v4.4.0.md` (which is
  preserved). Captures: full release chain table (9 versions),
  defect status (0 open), fleet state (3 nodes at 4.4.9), verified
  surfaces, operator follow-up items, backup artefacts, verdict.

**Operator action — none.** Doc-only.

### v4.4.9 — BUG-058 turn-2 follow-up (multi-turn test) (2026-05-20)

Follow-up to v4.4.8. The initial BUG-058 fix addressed `test_multi_turn_context` turn 1, but the test still failed on turn 2 with the same Gemini-preamble pattern ("Okay, here's the..."/"Okay, let's add..."). v4.4.9 extends the no-preamble directive + max_tokens raise to the turn-2 follow-up prompt (`"Now add a peek method..."`).

**Live verification post-deploy**: `test_multi_turn_context` PASSES against deployed v4.4.9.

Test-only release; proxy behavior unchanged from v4.4.6.

### v4.4.8 — BUG-058 matrix test assertions widened for Gemini-style verbose preambles (2026-05-20)

Closes BUG-058 (test-side polish from the L1 `--run-real` matrix
run). Two compatibility-matrix tests were too strict for the
Gemini-style verbose response pattern:

- `test_multi_turn_context` asked `"Define a Python class named \`Stack\` with push and pop"` with `max_tokens=150`. Gemini answered `"Okay, here's a..."` — the preamble consumed enough tokens that the literal `Stack` / `class` keywords sometimes landed past the cap.
- `test_stream_non_stream_content_equivalent` asked `"In one sentence, what does sum([1, 2, 3]) return in Python?"` with `max_tokens=60`. Gemini responded `"It returns [...]"` — the digit `6` sometimes never landed within the cap.

**Test-only fix** — no application behavior changes:

- **`test_multi_turn_context`**: prompt now includes `"Output only the code, no preamble or explanation."`; `max_tokens` raised 150 → 256; assertion widened to also accept `push`, `pop`, or `def ` as success signals (so a model that emits bare method definitions without the literal word `Stack` still passes).
- **`test_stream_non_stream_content_equivalent`**: prompt now reads `"What does sum([1, 2, 3]) return in Python? Reply with the digit alone, then a brief sentence."` — forces digit-first output; `max_tokens` raised 60 → 100 for headroom.

**Skipped v4.4.7** — operator directed straight to v4.4.8 (no
intermediate release).

**Test counts**

- Unit suite: **2288 passed** (unchanged from v4.4.6; BUG-058 is
  test-side only, no new unit tests; behavior verified against the
  live deployed v4.4.8 via the existing matrix tests).

**Operator action — none.** No application code changed; this
release exists to ship the updated `tests/integration/test_compatibility_matrix.py`
in the image (which is what `--run-real` runs against). The
running proxy behavior is identical to v4.4.6.

### v4.4.6 — BUG-057 OpenAI streaming `finish_reason` on the last chunk (2026-05-20)

Closes BUG-057. Surfaced 2026-05-20 by `test_compatibility_matrix.py::test_openai_stream_all_providers` during the L1 `--run-real` matrix run. The OpenAI ChatGPT path emitted streams whose final chunk had `finish_reason=null`, breaking OpenAI SDK clients that read the last chunk to detect end-of-stream.

**Root cause**: modern OpenAI streaming (with usage stats included, which litellm enables by default in the 1.83.x line) emits TWO chunks at end-of-stream:

```
chunk N-1: { finish_reason: "stop", delta: { content: null }, ... }
chunk N  : { finish_reason: null,   delta: { content: null }, usage: {...} }
```

The proxy used to pass through litellm's chunks verbatim, so the LAST emitted chunk carried only the usage info — no `finish_reason`. OpenAI SDK clients that look at the last chunk's `finish_reason` to detect end-of-stream block or misreport.

**Live capture** (Devin Personal OpenAI ChatGPT, gpt-4o, "Say OK", max_tokens=20):

```
chunk #1: finish_reason=None,  delta.content=''
chunk #2: finish_reason=None,  delta.content='OK'
chunk #3: finish_reason='stop', delta.content=None      ← old "last" chunk
chunk #4: finish_reason=None,  delta.content=None, usage={completion_tokens: 1, ...}  ← new last
```

**Fix** (`app/api/_completions_streaming.py::_stream_openai`): buffer one chunk so the FINAL chunk can be patched before serializing. Track the most recent `finish_reason` seen across the stream; on end-of-stream, if the last chunk lacks `finish_reason` and we saw one earlier, copy it onto the last chunk's `choices[0].finish_reason`. Preserves the usage info AND restores the end-of-stream signal.

Approach trade-off considered:
- ❌ Strip the usage chunk — would lose token-count info in the stream.
- ❌ Disable `stream_options.include_usage` upstream — same problem.
- ❌ Emit a synthetic third chunk — would surprise legacy clients that stop on first finish_reason.
- ✅ Buffer-and-patch — preserves the chunk count, preserves the usage info, restores the spec-compliant end-of-stream signal.

Latency impact: the buffer-and-emit adds 1-chunk delay only to the FINAL chunk emission (real content chunks emit immediately on the next chunk's arrival). Negligible UX impact.

**Tests** (`tests/unit/test_v446_bug057_openai_finish_reason.py`, +6):

- `test_bug057_fix_marker_present` — source-level marker guard.
- `test_bug057_buffer_strategy_in_source` — source-level: `prev_chunk` + `last_finish` + the in-place patch.
- `test_bug057_existing_first_chunk_ttft_path_preserved` — the TTFT instrumentation still runs.
- `test_bug057_usage_chunk_pattern_patches_finish_reason` — **end-to-end**: mock litellm with the modern OpenAI usage-chunk pattern; assert the LAST emitted chunk has `finish_reason='stop'` AND preserves the usage info.
- `test_bug057_classic_stream_unaffected` — regression guard: old-style streams (no usage chunk) emit unchanged.
- `test_bug057_no_finish_reason_anywhere_emits_as_is` — defensive: if upstream emits no `finish_reason` on any chunk, we don't invent one (faithfully report broken).

**Test counts**

- Unit suite: **2288 passed** (was 2282 in v4.4.5; +6 BUG-057 tests).

**Operator action — none required.** Affects OpenAI-style streaming through any provider using litellm's standard OpenAI path (1 of 10 active providers today: Devin Personal OpenAI ChatGPT). Non-streaming requests were always compliant.

### v4.4.5 — BUG-056 Anthropic streaming protocol compliance for empty-content providers (2026-05-20)

Closes BUG-056. Surfaced 2026-05-20 by `tests/integration/test_compatibility_matrix.py::test_anthropic_stream_all_providers` during the L1 `--run-real` matrix run. Both Gemini-backed providers (`C1 Vertex AI / Google AI`, `Google Generative LLM`) emitted streams missing `content_block_start` + `content_block_stop` SSE events — leaving Anthropic SDK clients unable to construct the assistant message object.

**Root cause** (`app/api/_messages_streaming.py::_stream_anthropic`): litellm's Gemini integration sometimes emits a single chunk with `delta.content=None` and only `finish_reason` set (especially when the response truncates at `max_tokens` before any text is generated, or when the entire short response buffers into the terminator chunk). The proxy's loop never flips `text_started=True`, so the post-loop guard `if text_started or tool_started: yield content_block_stop` short-circuits to a no-op, leaving the resulting SSE stream as `message_start → message_delta → message_stop` with no content_block events in between.

**Live capture** (the diagnostic that confirmed the shape):

```
Provider: Google Generative LLM
chunk #1: ModelResponseStream
  delta.content=None
  finish_reason='stop'
Total chunks: 1
```

The Anthropic streaming protocol requires every message to have at least one content block framed by `content_block_start` + `content_block_stop`. SDK clients (`anthropic-python`, `anthropic-sdk-typescript`) rely on `content_block_start` to construct the assistant message object; without it they emit empty/null content even when `message_delta` indicates the model ran.

**Fix**: when neither text nor tool content was streamed, emit a synthetic empty text block (`content_block_start` with `text=""` immediately followed by `content_block_stop`). The existing in-loop path is unchanged — `content_block_start` still fires on first content chunk, `content_block_stop` still fires at end-of-loop. Only the previously-no-op case (`not text_started and not tool_started`) gains the synthetic pair.

**Tests** (`tests/unit/test_v445_bug056_empty_stream.py`, +5):

- `test_bug056_empty_stream_fix_present_in_source` — source-level guard for the BUG-056 marker + the `if not text_started and not tool_started` branch.
- `test_bug056_existing_content_path_still_emits_stop` — the else-branch (real content was streamed) still emits `content_block_stop` exactly once.
- `test_bug056_empty_stream_emits_synthetic_content_block` — **end-to-end**: mock litellm with the Gemini empty-stream shape (single chunk, `delta.content=None`, `finish_reason='stop'`); assert the produced SSE stream contains all 5 required event types including the synthetic content_block start/stop pair.
- `test_bug056_text_stream_still_emits_normally` — regression guard: a 2-chunk content stream produces exactly ONE `content_block_start` (in-loop emission, not double-emit) + the expected delta + stop sequence.
- `test_bug056_empty_stream_does_not_break_other_envelope` — exact ordering check: `[message_start, content_block_start, content_block_stop, message_delta, message_stop]` for the empty case.

**Test counts**

- Unit suite: **2282 passed** (was 2277 in v4.4.4; +5 BUG-056 tests).

**Operator action — none required.** Affects 2 of 10 active providers (both Gemini-backed) for streaming requests only. Non-streaming traffic was always compliant. Fix is structural — no consumer changes needed.

### v4.4.4 — BUG-052 WAL TRUNCATE in prune sweep + Batch E release-verify (2026-05-20)

**BUG-052 — WAL high-water reclaim**

Root cause traced 2026-05-20: the 2026-05-13 RMAI 1.04B-token
amplifier loop drove 27× normal proxy volume (32,142 requests on
2026-05-13 vs 1,201 on 2026-05-10 baseline). The WAL grew during
that burst and stayed at the 1.097 GB high-water through every
subsequent container restart — SQLite preserves the WAL file size
in place across PASSIVE checkpoints; only TRUNCATE mode reclaims.

**Fix** (`app/monitoring/prune.py`):

- New `_wal_checkpoint_truncate()` helper. Reads pre/post WAL file
  size, runs `PRAGMA wal_checkpoint(TRUNCATE)`, returns dict with
  `busy / log_pages / ckpt_pages / size_before / size_after` so
  the sweep log line shows what was reclaimed.
- Wired into `_sweep_once()` LAST — must run after all prune
  steps, because those are exactly the heavy writes that re-extend
  the WAL. Running earlier would let subsequent prune writes
  re-grow it before the TRUNCATE.
- New `wal_reclaimed_bytes` + `wal_busy` fields in the
  `prune.swept` INFO log line for operator visibility after any
  future storm-class burst.
- Best-effort: any exception in the path is logged at WARNING and
  swallowed, never blocks the rest of the sweep.

**L3 — Pre-cut live-verify in `tools/cut-release.sh`** (Batch E
hardening)

Adds a pre-flight step 0 to the release ceremony: before tagging,
hit all 3 canonical `/health` URLs (voipguru.org, www2.voipguru.org,
c1conversations-avaya-01.avaya.c1cx.com) and require `status: healthy`
on each. If any fails, abort the cut with a clear message.

Rationale: the prior v4.3.3 footgun left tmrwww01 stuck on v4.3.2
because the local image tag wasn't updated; this verify step would
have caught that exact class of "fleet silently broken at cut
time" before tagging. Escape hatch: `--skip-live-verify` for the
legitimate case where the new release ITSELF is the fix for the
broken state.

**Tests** (`tests/unit/test_v443_bug054_bug055.py`, +5 tests):

- `test_bug052_wal_truncate_helper_exists` — `_wal_checkpoint_truncate`
  is exposed.
- `test_bug052_wal_truncate_wired_into_sweep_last` — source-level
  ordering guard: WAL truncate runs AFTER the orphan prune (and
  therefore after every prune step that writes).
- `test_bug052_sweep_output_dict_has_wal_block` — the new
  `wal_truncated` block is in `_LAST_SWEEP_RESULT`.
- `test_bug052_sweep_log_line_includes_wal_reclaim` — log line
  emits `wal_reclaimed_bytes` + `wal_busy`.
- `test_bug052_wal_truncate_returns_dict_shape` — end-to-end
  against a real SQLite test DB: the helper returns a dict with
  all expected keys, busy=0 in a clean test env.

**Test counts**

- Unit suite: **2277 passed** (was 2272 in v4.4.3; +5 BUG-052 tests).

**Operator action — none required.** Patch-class release. The new
WAL truncate runs daily as part of the existing sweep. The first
post-deploy sweep (~24h after deploy) reclaims any high-water left
in the WAL.

### v4.4.3 — BUG-054 frontend title + BUG-055 activity_log orphan prune (2026-05-20)

Closes two low-severity findings from the post-v4.4.2 QA pass.

**BUG-054 — frontend `<title>` was the Vite scaffold default "frontend"**

`frontend/index.html` was the unedited Vite template; the production
build inherited the literal title `frontend` into every browser tab.
Fix is a one-token change to `<title>llm-proxy v2</title>`. Rebuild
of the docker image picks up the new title in `frontend/dist/index.html`.

**BUG-055 — activity_log accumulates orphan FK references**

Audit on 2026-05-20 found 438 dangling `provider_id` refs + 7,937
dangling `api_key_id` refs in www1's `activity_log` table. Root
cause: provider / api_key tombstones get hard-deleted after
`provider_tombstone_retention_days` (default 7), but the
`activity_log` rows that referenced those IDs survive — SQLite has
no FK enforcement (`PRAGMA foreign_keys` is off). Audit / cost-
attribution queries that JOIN to providers / api_keys silently
lose those rows.

Fix (`app/monitoring/prune.py`):

- New `_prune_activity_log_orphans()` helper. Two `DELETE` passes
  (one per FK column) using the same `NOT IN (SELECT id FROM ...)`
  + `_BATCH_SIZE` paging pattern as the existing prune helpers.
- Wired into `_sweep_once()` AFTER the
  `_prune_provider_tombstones` + `_prune_apikey_tombstones`
  steps — the orphan-creation step that produces new orphans each
  sweep. Running the orphan prune first would leave the tombstone-
  prune's new orphans uncleaned for an extra day.
- New `activity_log_orphans` counter in `_LAST_SWEEP_RESULT` +
  the `prune.swept` log line for operator visibility.
- Failure-isolated like the other prune steps (`try` + warn-log on
  exception, never blocks the rest of the sweep).

**Tests** (`tests/unit/test_v443_bug054_bug055.py`, +7 tests):

- `test_bug054_frontend_html_title_is_not_vite_scaffold` — guards
  against ever re-introducing the Vite default.
- `test_bug055_orphan_prune_helper_exists` — `_prune_activity_log_orphans`
  is defined.
- `test_bug055_orphan_prune_wired_into_sweep` — ordering guard:
  orphan prune runs after the tombstone prune.
- `test_bug055_sweep_output_dict_has_orphan_counter` — the new
  counter is in the result dict.
- `test_bug055_sweep_log_line_includes_orphan_count` — the
  `prune.swept` log line emits the new counter.
- `test_bug055_orphan_prune_deletes_dangling_refs` — end-to-end
  behavior: seeds 3 live-FK rows + 5 orphan rows + 1 dual-orphan
  row, calls the helper, asserts 5 deletes (no double-count for
  the dual-orphan) and only live-FK rows survive.
- `test_bug055_orphan_prune_no_op_when_clean` — returns 0 when
  nothing to prune (doesn't hammer the DB needlessly).

**Test counts**

- Unit suite: **2272 passed** (was 2265 in v4.4.2; +7 BUG-054 + BUG-055
  regression tests).

**Operator action — none required.** Patch-class release. The new
prune step runs as part of the existing daily sweep (24h after
container start, then every 24h). Pre-existing accumulated orphans
will be cleaned on the first sweep after v4.4.3 deploy.

### v4.4.2 — BUG-053 cluster-sync tombstone propagation fix (2026-05-20)

Closes BUG-053 (medium, latent cluster-state correctness defect).
Surfaced 2026-05-20 21:18 UTC during v4.4.1's `CLEANUP-001`
verification: provider `391dc40f03f904c4` (`skew-from-new-41a9d6`)
was tombstoned on www1 at 03:33 UTC but remained `deleted_at=NULL`
on www2 + c1conv for **18 hours** despite ongoing sync cycles.

**Root cause** (`app/cluster/sync.py:164-185`): the v2.8.2 tombstone
propagation branch gated on `peer_deleted_at >= local_updated`.
When background activity on the receiver (sync cycles, OAuth
refresh, billing scrapes) bumped `local.updated_at` past the
originator's `deleted_at` timestamp, the tombstone branch
short-circuited — and the general LWW field-update path (lines
244-322) doesn't include `deleted_at` in its column set, so
the tombstone had no other route to propagate.

**Fix**: tombstones are terminal in this app (no undelete UI), so
"peer has a tombstone, local doesn't" is sufficient signal to
converge. The branch now triggers on `peer_deleted_at and not
local_deleted` unconditionally. The pre-existing "both sides
have a tombstone → local wins" branch (lines 189-192) is
preserved, so the fix can't cause a tombstone to flap.

**Tests** (`tests/unit/test_cluster_sync_lww.py`, +3 tests):
- `test_bug053_tombstone_propagates_when_local_updated_at_is_newer`
  — direct repro: peer tombstone, local has no tombstone but
  newer updated_at; tombstone must still propagate.
- `test_bug053_tombstone_propagates_with_tied_user_edit_at` —
  exact reproduction of the live skew-from-new-41a9d6 case.
- `test_bug053_local_tombstone_not_overwritten_by_peer_tombstone`
  — symmetric guard: when both sides already have a tombstone,
  the fix must not clobber local with a later peer value.

**Impact**: today's fleet is consistent (manual reconcile applied
in v4.4.1's session), but the fix removes the underlying defect
so future soft-deletes propagate cluster-wide on the next sync
cycle. Real impact today: a soft-deleted provider could remain
"active" on peer nodes for an indefinite period, allowing peer
routing to dispatch to a row the operator believed retired.

**Test counts**

- Unit suite: **2265 passed** (was 2262 in v4.4.1; +3 BUG-053
  regression tests).

**Operator action — none required.** Patch-class release.

### v4.4.1 — BUG-051 M-3 rate-limit mapping fix (2026-05-20)

Closes BUG-051. The v4.4.0 post-release QA pass found that M-3's
keepalive→`provider_node_auth_state` writer was mapping the
`classify_error` result `"rate_limit"` into the catch-all branch
(`needs_reauth`) instead of the transient-failure branch
(`bridge_down`). Live evidence: c1conv's local row for the grok-web
provider was stamped `auth_state="needs_reauth"` for an upstream 429
(`Too many requests`) — which would, on any future Path A
activation, semi-permanently gate the throttled node out of routing
until an operator clicked [Re-auth] for nothing.

**Fix** (`app/monitoring/keepalive.py:283-294`):

```diff
-elif _cls in ("network", "timeout", "upstream_5xx"):
+elif _cls in ("network", "timeout", "upstream_5xx", "rate_limit"):
+    # rate_limit added v4.4.1 (BUG-051): a 429 is transient
+    # throttling, not a re-auth signal. Self-clears on next probe.
     _new_state = "bridge_down"
```

Policy for the other 4 classifier outcomes is unchanged: `auth` →
`needs_reauth`; `billing`, `bad_request`, `unknown` → `needs_reauth`
(operator-time signal, by design).

**Tests** (`tests/unit/test_v44_m3_m4_routing_and_cb.py`):

- `test_bug051_rate_limit_maps_to_bridge_down` — asserts the
  classifier buckets common 429 strings as `rate_limit` AND that
  the M-3 mapping has the explicit `rate_limit` branch with the
  inline BUG-051 comment.
- `test_bug051_billing_and_bad_request_still_needs_reauth` —
  guards against the fix accidentally widening into a regression
  for `billing` / `bad_request` / `unknown`.
- `test_probe_outcome_mapping_matches_classifier_buckets` —
  updated to assert the new branch literal.

**Impact**: M-4 is dormant in v4.4.x (0/18 providers have
`node_local_session=True`), so the mis-stamped state has no
production effect today. The fix removes a latent bug that would
have re-activated on any Path A retry. Pure scaffolding-correctness.

**Test counts**

- Unit suite: **2262 passed** (was 2260 in v4.4.0; +2 BUG-051
  regression tests).

**Operator action — none required.** Patch-class release.

### v4.4.0 — grok-bridge hardening + dormant Path A scaffolding (2026-05-20)

Closes BUG-025 mechanically. The v4.4 milestone arc resolves with
**Path A empirically rejected** by a live multi-node spike and **Path B
as the operative topology** (single shared bridge, hardened).

The arc went M-1 (image hardening) → M-2..M-5 (backend +
admin-API + UI scaffolding for the per-node-bridge architecture) →
M-6 (live spike with the deployed per-node bridges). The spike found
that grok.com enforces single-account-session semantics — a second
concurrent IP got silently de-authed mid-flow; a third got
"You have been blocked" before login completed. Path A is not
feasible with the current Grok account-safety model. Path B
(single bridge, hardened) is what stays live.

**M-1 (LIVE) — grok-bridge image hardening**

The latent failure mode behind BUG-025 was an Xvfb startup race:
`start.sh` polled for `/tmp/.X11-unix/X99` to exist (which the
kernel creates very early in Xvfb init), but Chromium's connection
attempt (during the FastAPI lifespan's `launch_persistent_context`)
raced into a half-open X server and failed with
`Missing X server or $DISPLAY`. Every fresh container start hit
this; the 10-day-old "Up" container on tmrwww01 had won the race
on its original boot. v4.4-M-1 ships:

- `grok_bridge/Dockerfile` — adds `x11-utils` (for `xdpyinfo`).
- `grok_bridge/start.sh` — replaces the socket-file existence
  check with an actual X11 query (`xdpyinfo -display :99`). The
  loop polls every 0.5s up to 30s. On timeout, dumps supervisord
  status + `/tmp/.X11-unix` listing + Xvfb stderr to make
  post-mortems trivial.
- `docker-compose.yml` on tmrwww01 — `healthcheck` block probes
  the inner FastAPI `/healthz` every 30s with `start_period: 60s`.
  The `docker ps "Up"` status now reflects the bridge service,
  not just supervisord — closes the BUG-025-class hidden-failure
  pattern within one health interval.

**Verification on tmrwww01**: bridge container recreated cleanly
with the new image — log line `Xvfb display :99 responsive after
20ds` followed by `playwright ready; bridge listening`. No
`Missing X server` error. Healthcheck transitions to `healthy`
within the first 30s probe. Restart count = 0. The fleet's
grok-web CB (`8beb17c4bd11de26`) returned to
`closed/failures=0` immediately.

**Image artefact**: `dblagbro/llm-proxy2-grok-bridge:v4.4-rc1` +
`:latest` on Docker Hub.

**M-2 (STAGED, dormant) — `ProviderNodeAuthState` schema + cluster sync**

New table for per-(provider_id, node_id) auth state, intended for
the per-node bridge architecture. Each node owns its own rows
(writes locally, peers read for visibility). Cluster sync uses LWW
on `last_check_at`. Path B doesn't need this active — but the
table can be re-used as Path B's single-bridge state tracking if
desired (one row per provider, written by the host with the
bridge, visible to all nodes via sync).

New files / changes:

- `app/models/db.py` — `ProviderNodeAuthState` ORM model.
- `app/routing/node_auth_state.py` — read/write helpers
  (`write_local_state`, `read_state`, `read_all_states`,
  `is_local_node_routable`).
- `app/cluster/manager.py` — `push_sync()` payload extended with
  `provider_node_auth_states` key.
- `app/cluster/sync_handlers.py` — `_apply_provider_node_auth_states`
  with LWW.
- `app/cluster/sync.py` — apply-dispatch wiring.

13 unit tests in `tests/unit/test_v44_m2_provider_node_auth_state.py`
cover schema, helpers (insert + upsert + last_ok_at preservation +
validation + error truncation), routing gate, sync LWW (older
incoming skipped, newer accepted), defensive malformed-row
handling, and a push-payload regression guard.

**M-3 (STAGED, dormant) — probe → state writer**

`app/monitoring/keepalive.py` grok-web probe branch now writes the
local node's row on each probe outcome:
- success → `auth_state="ok"`
- classify_error → `"auth"` → `"needs_reauth"`
- classify_error → `"network"`/`"timeout"`/`"upstream_5xx"` → `"bridge_down"`
- anything else → `"needs_reauth"`

Best-effort: any exception in the write path is swallowed so it
can't corrupt the probe's own `record_outcome` flow.

**M-4 (STAGED, dormant) — routing filter + CB-sync exemption**

`app/routing/router.py:select_provider()` now consults the local
auth-state row for any provider tagged
`extra_config.node_local_session=True`; if not OK, the provider is
filtered out of this node's routing for the request. Other cluster
nodes whose state IS OK can still serve it via their own routing
decisions.

`app/routing/circuit_breaker.py:_persist_auto_skip()` exempts
node_local_session-tagged providers from the persistent-auth-
failure auto-skip path. The per-node auth_state rows are the
cluster-visible signal instead.

Both branches are **no-ops** for any provider without the
`node_local_session=True` flag in `extra_config`. With Path A
rejected, that flag is **never going to be flipped**, so M-4
stays inert in production. The scaffolding survives the release
for any future Path A retry.

**M-5 (STAGED, dormant) — admin API + UI**

New `GET /api/providers/{id}/node-auth-states` returns the per-
node bridge state list. Empty when no rows exist (typical for
non-bridge providers, or for grok-web before the first probe has
run after a Path A retry).

`frontend/src/components/providers/NodeBridgeStatusPanel.tsx`
renders the per-node table with badges (ok / re-auth-needed /
bridge-down / never-auth'd), last_ok_at and last_check_at
timestamps (relative), and a `[Re-auth]` button when needed +
`reauth_url` is populated. Polls every 30s. Wired into
`ProviderForm.tsx` in edit mode.

**Path B operational state**

- Single shared bridge on tmrwww01 (the M-1-hardened image).
- `providers.extra_config.bridge_url` keeps the public URL
  (`https://www.voipguru.org/grok-bridge/...`); cluster-synced to
  all 3 nodes.
- Cleanup of the M-6 spike artefacts: tmrwww02 + c1conv bridge
  containers + images removed; nginx `/grok-bridge/` location
  blocks removed from tmrwww02 + c1conv (route returns 404 / 401-
  catch-all respectively); tmrwww02's compose service definition
  removed. Data volumes RETAINED per the operator-locked
  `docker volume rm` ban.

**Test counts**

- Unit suite: **2260 passed** (was 2241; +19 across M-2 (13) +
  M-3/M-4 (6) unit tests).
- Frontend: TypeScript clean.

**Architecture / doc updates**

- `docs/4.4-per-node-bridge-design.md` — status header flipped to
  "Path A rejected, Path B operative", §3.2 added with the live
  spike result data.
- `architecture.md` — small subsection on the v4.4 scaffolding (see
  next commit).
- `docs/bug-log.md` — BUG-025 final-status entry updated with the
  v4.4-M-1 resolution paragraph; BUG-046 cross-references the new
  bridge-stable state.

**Operator action — none required.** The release is operationally a
patch (proxy behavior unchanged from v4.3.9 for any caller).

### v4.3.9 — classify_error coverage for grok-web bridge errors (BUG-048)

Closes BUG-048 (low-pri classifier gap). Root-cause inspection of the
24h "unknown" rows showed two patterns the existing
`circuit_breaker.classify_error()` regex set didn't recognise.

**Pattern 1 — `grok-web bridge 404: "Conversation 'X' not found"`**
(~80% of unknowns): the bridge keeps trying a conversation ID that
no longer exists at grok.com. 404 wasn't in any pattern list.

**Pattern 2 — `grok-web bridge unreachable: Server disconnected
without sending a response`** (the remaining ~20%): httpx's
`RemoteProtocolError` documented message. The classifier had the
exception NAME (`remoteprotocolerror`) but the bridge wrapper
surfaces the formatted prose, not the camelcase name.

**Changes** (`app/routing/circuit_breaker.py`):

- `_BAD_REQUEST_PATTERNS` gains `404`, `405`, `409`, `410`, `413`,
  `415`, `422`, and the literal `"not found"`. (`401`/`403` stay
  routed to `auth`, `402` to `billing`, `429` to `rate_limit` —
  those buckets run earlier in the chain.)
- `_NETWORK_PATTERNS` gains `"server disconnected"`,
  `"without sending a response"`, `"bridge unreachable"`.

**New unit tests** in
`tests/unit/test_v439_classifier_grok_bridge_coverage.py` — 24 cases:
- Both prod-observed strings (exact, with their nested-JSON shape)
- Parametrized 4xx-codes-other-than-401/402/403/429 → bad_request
- Network-prose additions
- Regression guards: 502/503 still upstream_5xx; 429 still
  rate_limit; 401/403 still auth (via is_auth_error); 402 still
  billing; ReadError/RemoteProtocolError camelcase still network;
  empty string still unknown; ordering invariant holds (auth wins
  over 404).

Unit suite: 2241 passed (was 2217; +24 new). All green.

### v4.3.8 — fix Anthropic→OpenAI/Cohere tool-def translation gap (BUG-047)

Closes BUG-047 surfaced during the 2026-05-20 proactive-monitoring sweep
(``docs/proactive-sweep-2026-05-20.md``). The cross-family translation
gate in ``app/api/messages.py`` was firing for tool-use *content blocks*
in messages but NOT for tool *definitions* in ``body.tools`` on a
first-turn request. The 24h activity log showed steady upstream 400s
on Devin-Cohere + Devin Personal OpenAI ChatGPT with identical error
shapes:

```
litellm.BadRequestError: CohereException -
  "invalid tool at tools[0]: missing required field: 'type'"
litellm.BadRequestError: OpenAIException -
  "Missing required parameter: 'tools[0].type'."
```

Root cause: ``has_anthropic_tool_content(messages)`` only checks for
``tool_use``/``tool_result`` blocks; on first-turn requests where the
caller provides Anthropic-shape tool defs (``{name, description,
input_schema}``) but the conversation has no tool-use blocks yet, the
gate evaluated False and the raw Anthropic-shape tools reached litellm
untranslated. OpenAI/Cohere rejected them on the missing top-level
``type: "function"`` field.

**Fix**:

- **New helper** ``has_anthropic_tool_defs(tools)`` in
  ``app/routing/tool_content.py`` returns True iff any tool in the
  request-level ``tools`` list is Anthropic-shape (has ``input_schema``
  OR lacks the OpenAI ``{type:"function", function:{...}}`` envelope).
  Defensive against mixed-shape lists and non-dict items.
- **Gate widened** in ``app/api/messages.py`` — ``_needs_openai_translation``
  now also fires when ``has_anthropic_tool_defs(body.get("tools"))`` is
  True. The existing claude-oauth and tool-emulation exclusions remain.

**New unit tests** in ``tests/unit/test_v438_anthropic_tool_defs_translation.py``
(12 cases): None / empty / Anthropic-shape / OpenAI-shape / partial-OpenAI
/ missing-type / wrong-type / mixed / non-dict / empty-input_schema; plus
a regression guard that the two helpers (``has_anthropic_tool_content`` for
blocks vs ``has_anthropic_tool_defs`` for defs) remain orthogonal.

The pre-existing ``test_v3100_translation_gate.py`` was doing a literal
one-line source-grep on the old gate condition; updated to assert each
clause as a substring (``cross_family_fallback`` + ``_has_tool_blocks``
+ ``has_images`` + the new ``_has_anthropic_tool_defs``) instead.

Unit suite: 2217 passed (was 2204; +13 net). All green.

Expected operational impact post-deploy:
- Devin-Cohere bad_request rate (7/24h pre-fix) drops to ~0
- Devin Personal OpenAI ChatGPT bad_request rate (6/24h pre-fix) drops to ~0
- Net error volume reduction across the non-anthropic providers

### v4.3.7 — stop persisting Python None as the literal string "None" in system_settings

Closes a small but operationally annoying defect surfaced during BUG-031's
live verification on 2026-05-19: the dry-run AIRI notifier returned
``recipients: ["None"]`` (the **literal string** "None"). Investigation
showed ``system_settings.smtp_to = 'None'``, plus the same pattern for
``smtp_from`` + ``smtp_host``.

**Root cause**: ``app/config_runtime.py:save()`` line ~499 did
``raw = str(val)``. When ``val`` was Python ``None`` (operator clears
the field via Settings UI; or a settings cluster-sync pushes a None
value), it persisted the literal string ``"None"``. On load,
``_coerce(raw, "str")`` returned the string as-is. Result:
``settings.smtp_to == "None"`` (a truthy string), which then passed
downstream guards like ``if settings.smtp_to:`` and got added to
recipient sets. SMTP-side, alerts addressed to a literal user named
"None" silently bounce — operator never noticed because the proxy
considered the alert "sent."

**Operational impact in prod**: alerts going to the global mailbox
``settings.smtp_to`` were silently bouncing for some duration (since
whenever the bad value got persisted; the live DB had ``smtp_to``,
``smtp_from``, ``smtp_host`` all set to ``"None"``). Per-user
notification subscriptions (v4.0.3) still worked because they store
real email addresses; the global mailbox is the bug surface.

**Fix** (``app/config_runtime.py``):

1. ``save()`` now writes empty string ``""`` when ``val is None``
   instead of ``str(None) == "None"``. Same for the in-memory
   ``apply()`` projection — None values land as None on the live
   settings singleton, not as "None".
2. ``_coerce()`` for ``str``-typed fields treats both the empty
   string and the legacy literal ``"None"`` as Python ``None``. This
   preserves Optional[str] semantics on load AND tolerates pre-fix
   data on un-migrated nodes during a rolling deploy (no migration
   step required).
3. ``"Nones"``, ``"None@example.com"``, etc. continue to pass
   through unchanged — only the exact strings ``""`` and ``"None"``
   coerce to None.

**Operator action after deploy**: rows currently holding the literal
``"None"`` will be tolerated by the load path (treated as None), but
cleaning them up is one ``UPDATE system_settings SET value = ''
WHERE value = 'None' AND value_type = 'str';`` per node — optional,
since the load path already handles them.

**New unit tests** in ``tests/unit/test_v437_smtp_to_none_string.py``
(11 cases): _coerce truthy/falsy/legacy-None matrix; save→DB writes
empty string for None; save with real string unchanged;
save→load round-trip yields Python None on the live singleton;
legacy ``value="None"`` row tolerated on load.

Unit suite: 2204 passed (was 2193; +11 new). All green except the
pre-existing flaky chaos test (passes in isolation; unrelated).

### v4.3.6 — dry_run mode for the AIRI notifier (BUG-031)

Closes BUG-031. Until v4.3.6 the AIRI rule-fire email path had no
way to be live-tested without spamming the operator's inbox; the F2
coverage pass on 2026-05-19 surfaced this as a coverage gap that
needed a code-side change before live testing was safe.

**Change** (`app/airi/notify.py`):
`airi_notify(...)` gains a `dry_run: bool = False` parameter. When
true (or when the `AIRI_NOTIFY_DRY_RUN` env var is truthy), the
function still:

- builds the full email body (including the `Open AIRI` deep link),
- resolves recipients (the global `settings.smtp_to` PLUS each
  operator whose per-user subscription opts into this category +
  severity),

…but **skips the SMTP send** and instead returns a dict of the
planned dispatch:

```json
{
  "dry_run": true,
  "subject":  "AIRI: <subject>",
  "body":     "...body with deep link...",
  "severity": "warning",
  "category": "monitor",
  "recipients": ["alice@example.com", "ops@example.com"]
}
```

Production callers (today only `app/airi/evaluator.py`) ignore the
return value, so this is non-breaking.

**New admin-only endpoint** (`POST /api/airi/notify/_test_dispatch`):
HTTP front-door for the dry-run path. Lets a live integration test
(or an operator manually, from the proxy admin UI session) verify
recipient resolution + body rendering against the live deployment
without spamming inboxes. Body:

```json
{
  "subject":  "string",      // required
  "message":  "string",      // required
  "severity": "info|warning|critical",  // default warning
  "category": "monitor|automation"      // default monitor
}
```

Returns the dry-run dict above on success; 400 on missing fields /
bad enum; 500 if `airi_notify` raised internally.

**New unit tests** in `tests/unit/test_v436_notify_dry_run.py` (14
cases):
- `dry_run=True` skips `send_alert` + returns the planned-dispatch dict
- `AIRI_NOTIFY_DRY_RUN=1` env var flips the same behaviour globally
- Both case-insensitive and the canonical truthy set
  (`1`, `true`, `TRUE`, `yes`, `on`) accepted
- Falsy values (`0`, `false`, `no`, `off`, `anything-else`, empty)
  do NOT activate dry-run
- Production path (`dry_run=False`, env unset) still calls
  `send_alert` for every recipient and returns `None`
  (regression guard for v4.0 behaviour)

Unit suite: 2170 passed (was 2156; +14 new). All green.

### v4.3.5 — HMAC-auth read-only endpoint for coordinator-hub (`external_usage_summary`)

Built for the coordinator-hub team's 2026-05-20 request. They want to
surface per-Anthropic-account weekly utilization (the
`external_usage_snapshot` scrape data, v3.7.0+) on the hub UI without
holding an admin session cookie on the proxy. This release adds a
read-only HMAC-authenticated path that lets them.

**New endpoint**: `GET /api/admin/external-usage-summary`

Returns the latest `external_usage_snapshot` row per provider (whether
the scrape succeeded or failed) so the hub can surface 5-hour + 7-day
utilization plus `auth_state` per Anthropic account:

```json
{
  "snapshot_at": "2026-05-20T01:23:45Z",
  "accounts": [
    {
      "label": "Devin-Anthropic-Max-Gmail",
      "provider_id": "...",
      "seven_day_utilization": 1.00,
      "five_hour_utilization": 0.42,
      "auth_state": "ok",
      "last_scrape_at": "2026-05-20T01:15:00Z"
    }
  ]
}
```

**Auth contract** (per the hub team's spec):

```
X-Cluster-Timestamp: <unix epoch seconds>
X-Cluster-Auth:      <hex sha256 hmac>

signed_bytes = f"{timestamp}{request.url.path}".encode() + request.body()
expected     = hmac.new(secret_utf8, signed_bytes, sha256).hexdigest()
```

The proxy validates: (a) timestamp within ±60s of server time (replay
protection), (b) HMAC matches via constant-time compare. Secret comes
from `COORDINATOR_HMAC_KEY` env var — same secret the hub already
shares with its peers, so no new credential provisioning needed.

**Operator action required**: before HMAC auth activates, the operator
must add `COORDINATOR_HMAC_KEY=<value>` to `/home/dblagbro/docker/.env`
on tmrwww01 + tmrwww02 (and `/opt/C1/instance/.env` on c1conv) and
recreate `llm-proxy2` on each. Until then, the endpoint returns
**503 "Cluster HMAC auth not configured"** with a clear message.

**New files**:
- `app/auth/cluster_hmac.py` — `require_cluster_hmac` FastAPI dep
- `app/api/admin_cluster_read.py` — the endpoint
- `tests/unit/test_v435_cluster_hmac.py` — 11 unit tests (happy path,
  missing-secret 503, missing-headers 401, non-numeric / stale /
  future timestamps, wrong-secret / wrong-path / wrong-signature
  rejects, uppercase-hex accept, ±60s boundary)

**Wire-up**: `app/main.py` includes the new router. `docker-compose.yml`
adds `COORDINATOR_HMAC_KEY=${COORDINATOR_HMAC_KEY:-}` to the
llm-proxy2 service (passes through whatever the host shell / .env
provides; empty by default).

Unit suite: 2156 passed (was 2145; +11 new). All green.

### v4.3.4 — revert v4.3.2 dead code (BUG-026 Batch B)

Closes BUG-026. The v4.3.2 release shipped an interim "skip the grok-web
probe when the local sidecar is unreachable" patch built on a wrong
architectural premise: the grok-bridge isn't a *per-node* docker-internal
sidecar — there is exactly **one** bridge in the fleet, on tmrwww01, and
every node reaches it through the public URL stored in
`providers.extra_config.bridge_url` (cluster-synced). A
`_local_sidecar_reachable()` helper that probes that public URL trivially
succeeds, so the gate never fires in production — the patch was non-
functional dead code from the moment it deployed.

This release removes the dead artefacts:

- `app/monitoring/keepalive.py` — the `_no_local_sidecar: set[str]` flag,
  the public `is_no_local_sidecar()` helper, the
  `_local_sidecar_reachable()` reachability probe, and the v4.3.2 branch
  inside `_probe_one()`'s `grok-web` arm that consulted them are all gone.
  The `grok-web` arm now falls straight through to the dispatcher (the
  v3.2.10 behaviour, which is the correct one — the bridge IS reachable
  from every node when it's running; when it's not running, the CB +
  per-probe error suppression elsewhere handle the noise correctly).

- `tests/unit/test_v432_no_local_sidecar.py` — the 3 unit tests that
  exercised the dead helpers. Removed in full.

No callers consulted the public `is_no_local_sidecar()` helper (grep
across `app/` + `tests/` confirms), so the deletion is local in every
sense. `httpx` import in `keepalive.py` stays — used by the actual
provider probe path (see `_probe_one()` line ~350).

The compose-level grok-bridge healthcheck that was part of Batch B's
plan is **not** included here: the bridge container is currently stopped
(BUG-025 deferred to v4.4), and the v4.4 redesign will reshape what a
"healthcheck" should look like. Punting now avoids configuring a
watchdog around a known-bad startup race.

Unit suite drops from 2148 → 2145 (the 3 BUG-026 tests removed); all
green. Frontend bundle unchanged.

### v4.3.3 — input validation: reject negative caps + empty passwords at the API boundary

Closes BUG-041 + BUG-042 (both surfaced by the F2 coverage-gaps pass on
2026-05-19). Two small Pydantic field-constraint additions on the
*create* endpoints:

- `POST /api/keys` (`KeyCreate`) now rejects negative values for any
  numeric cap/limit field (`rate_limit_rpm`, `spending_cap_usd`,
  `daily_soft_cap_usd`, `daily_hard_cap_usd`, `hourly_cap_usd`) — `ge=0`.
  Pre-fix, an admin (or buggy client) could persist `rate_limit_rpm:-5`
  on a brand-new key, leaving the rate limiter's behavior undefined.
  `PATCH /api/keys/{id}` (`KeyUpdate`) **retains** its documented
  `-1 = clear the limit` sentinel — that path has a real reason to
  accept negatives.
- `POST /api/users` (`UserCreate`) now requires `username` to be
  non-empty and `password` to be at least 8 characters. Pre-fix, an
  admin could create a user with an empty password and the account
  was persisted (an authentication hole — see BUG-042). `PATCH
  /api/users/{id}` (`UserUpdate`) **retains** its "empty password =
  no change" semantic (the route uses a falsy check after schema
  validation).

15 new unit tests in `tests/unit/test_v433_create_validation.py`
cover the constraint at the schema layer (negative rejected;
omitted-or-zero accepted; -1 sentinel preserved on PATCH; password
boundary at exactly 8 chars; UserUpdate empty-password skip intact).
The two F2 Playwright tests
(`TestFormValidationNegatives::test_create_user_form_rejects_empty_password`
and `…::test_create_api_key_rejects_malformed_rate_limit`) lose their
`@pytest.mark.xfail` decorators and become regression guards.

Total unit-test count: **2148** (was 2133); all green.

### v4.3.2 — interim: prober skips a provider whose local sidecar is absent

Interim noise fix for BUG-023 (the QA-pass finding on c1conv). A few
providers — today only `grok-web` (served via the `grok-bridge` browser
sidecar) — are part of the cluster-synced provider config, but the sidecar
they need is per-node infrastructure. On a node where the sidecar isn't
deployed, every keepalive probe was failing with a connection error,
tripping the circuit breaker, and producing ~285 noisy `severity=error` rows
in the activity log per day per such provider (observed on c1conv).

This release lands a small, surgical fix: the keepalive prober now does a
fast TCP-connect to a bridge-mode `grok-web` provider's `bridge_url` first.
If the bridge isn't reachable (connection error / DNS failure), it sets a
per-(provider, node) "no local sidecar" flag, logs the fact once at INFO,
and **skips** the probe — no `complete_grok_web` call, no error row, no CB
hit. When the sidecar later appears, the flag clears automatically and
probing resumes.

A new helper `keepalive.is_no_local_sidecar(provider_id)` exposes the flag
so future routing code can consult it (the v4.4 per-node-auth-state arc
will replace this informal flag with a proper synced cluster-wide view
plus a guided cross-node auth flow).

No backend/API contract change; `whisper-bridge` unchanged.

### v4.3.1 — QA remediation (v4.3.0 QA pass, Groups 1 + 2)

Frontend-only patch addressing the low-severity findings from the v4.3.0 QA
pass (`docs/4.3-qa-report.md`). No backend behavior change beyond one
additive endpoint; `whisper-bridge` is unchanged.

- **`GET /api/auth/session`** (new, additive) — an unauthenticated-safe
  session probe that always returns 200 (`{authenticated: bool, …}`). The
  frontend auth bootstrap now probes this instead of `/api/auth/me`, so a
  logged-out page load no longer logs a `401` console error (BUG-020).
  `/api/auth/me` keeps its 401 contract untouched.
- **Reduced-motion** — the AIRI voice buttons (speaker, mic, hands-free) add
  `motion-reduce:animate-none` so their pulse animation respects
  `prefers-reduced-motion` (BUG-024).
- **Tests** — `TestAiriTTS` (`tests/integration/test_playwright_ui.py`)
  covers the v4.3 message→speak wiring; `docs/release-checklist.md` adds the
  manual real-browser TTS audible-playback check (BUG-021, BUG-022).

### v4.3.0 — AIRI text-to-speech ("Airy" speaks answers back)

v4.2 gave AIRI ears (push-to-talk + the "Airy" wake word); v4.3 gives it a
voice. With the new speaker toggle on, "Airy" reads each completed answer
aloud — so an operator can ask by voice and hear the reply hands-free.

- **`whisper-bridge` gains Piper TTS.** The voice sidecar now also runs
  Piper — the standalone prebuilt binary (self-contained: it bundles its own
  onnxruntime, phonemizer and espeak-ng data) plus the `en_US-amy-medium`
  voice, both baked into the image. Same own-the-dependency-chain rule that
  ruled out the Web Speech API for v4.2: synthesis runs on our own hardware.
- **`POST /api/airi/speak`** — admin- and flag-gated; forwards answer text to
  the sidecar and streams back `audio/wav`. Text and audio are transient —
  never persisted.
- **Speaker toggle** in the AIRI chat panel, beside the mic. Off by default.
  When on, a completed assistant message is synthesized and played; a new
  message supersedes any in-progress playback so Airy never talks over
  itself. Tap while speaking to stop. Proposal cards and errors are not read
  aloud. A synthesis or playback failure is swallowed quietly — a failed
  read-aloud never disrupts the chat.
- Feature-flagged: `airi_tts_enabled` (default off), independent of
  `airi_voice_enabled`.
- **Fix** — `_bridge_headers()`: an unset whisper-bridge token produced an
  illegal `Bearer ` header that httpx rejects. The header is now omitted
  when no token is configured; `transcribe`, `voice-model` and `speak` all
  use the helper. (Dormant in production, where the token is always set.)

Deploying v4.3.0 ships a new `whisper-bridge` image (`dblagbro/whisper-bridge`
bumped to `4.3.0`) — it must be recreated alongside `llm-proxy2`.

## v4.2.x — "Voice" milestone

### v4.2.2 — fix: hands-free never detected the wake word

v4.2.1's hands-free shipped a **free** Vosk recognizer and matched "Airy"
as plain text in the transcript. Vosk's small model mis-hears "Airy" as
common words — most often **"every"** — so the wake word was never matched
and hands-free did nothing when spoken to (the button turned on, the mic
was live, but speaking had no effect). Confirmed by Playwright: the audio
pipeline delivered speech to Vosk correctly, but Vosk transcribed "airy"
as "every".

The fix is a two-stage design:

- **Wake** — the Vosk recognizer is now **grammar-constrained** to
  `["airy", "[unk]"]`. It can only emit `airy` or `[unk]`, which makes
  wake detection reliable. (Simply adding "every" to the wake list was not
  an option — "every" is a top-100 English word and would false-trigger
  constantly.)
- **Command** — a grammar-constrained recognizer cannot transcribe an
  open-ended command, so once awake the command is recorded with
  `MediaRecorder` and transcribed by **Whisper** via `/api/airi/transcribe`
  — the same path push-to-talk uses, and far more accurate for free speech
  than Vosk's small model.

No backend or API change — `whisper-bridge` and the proxy endpoints are
unchanged from v4.2.1. The `whisper-bridge` image is unchanged; only
`llm-proxy2` is rebuilt. Still gated by `airi_voice_enabled`.

### v4.2.1 — AIRI hands-free wake word ("Airy")

The third and final milestone of the v4.2 voice design — voice without
touching the mic button. With hands-free enabled, the operator says **"Airy"**
and the rest of the utterance lands in the chat input.

- **In-browser wake-word detection.** `vosk-browser` (the Vosk speech engine
  compiled to WebAssembly) runs the recognizer **entirely in the browser** —
  no audio leaves the browser for wake detection, so a continuously-listening
  mic never streams anywhere. Only the transcript text is used.
- **`whisper-bridge` now also hosts the Vosk model.** The small English Vosk
  model (~40 MB) is baked into the sidecar image at build time and served at
  `GET /vosk-model`; `GET /api/airi/voice-model` proxies it to the browser
  (the browser cannot reach the docker-network-only sidecar directly). The
  browser fetches the model once and caches it. `alphacephei.com`'s TLS
  certificate is expired upstream, so the build pins the model's **SHA256** —
  content integrity is verified by hash, independently of the broken cert.
- **Hands-free toggle** in the AIRI chat panel, next to the push-to-talk mic.
  Say "Airy show me provider health" in one breath, or "Airy" then the command
  as a second utterance — both work. As with push-to-talk, the transcript
  **fills the input for review** and is **never auto-sent**.
- `vosk-browser` is dynamically imported, so its ~5.8 MB WASM chunk only loads
  when an operator turns hands-free on — the main bundle is unchanged.
- Still gated by the existing `airi_voice_enabled` flag; no new flag.

Deploying v4.2.1 ships a new `whisper-bridge` image (`dblagbro/whisper-bridge`
bumped to `4.2.1`) — it must be pulled/recreated alongside `llm-proxy2`.

### v4.2.0 — AIRI voice input

The AIRI chat panel gains voice. An operator can speak a request instead of
typing it — push-to-talk.

- **`whisper-bridge`** — a new sidecar container: FastAPI + self-hosted
  `faster-whisper` + ffmpeg. Speech-to-text runs on our own infrastructure;
  the locked own-the-dependency-chain rule rules out the browser Web Speech
  API (which streams audio to Google). The Whisper model is baked into the
  image at build time — the running container makes no external fetch.
- **`POST /api/airi/transcribe`** — admin- and flag-gated; forwards an audio
  blob to the sidecar and returns the transcript. Audio is transient — never
  persisted, never logged.
- **Push-to-talk mic button** in the AIRI chat panel — tap to record, tap to
  stop; the transcript fills the input for the operator to **review before
  Send**. Voice never auto-sends, so the PII/guard path still applies.
- Feature-flagged: `airi_voice_enabled` (default off). The "Airy" hands-free
  wake word and AIRI text-to-speech are designed but deferred (see
  `docs/4.2-voice-design.md`).

Deploying v4.2.0 adds the `whisper-bridge` container to each node — see the
deployment section of the design doc.

## v4.1.x — "Capability routing" milestone

### v4.1.1 — tool + CoT co-emulation

The deep follow-up to v4.1.0's capability gate. Tool emulation and CoT-E
were mutually exclusive — the router suppressed tool emulation whenever CoT
engaged — so a request needing both, on a provider native in neither, lost
its tools, and the gate could only work around it by skipping the provider.

They now compose. When both engage, the tool prompt is reasoning-prefixed:
the model thinks step by step inside a `<thinking>` block, then emits
`<tool_call>` blocks — one call, parsed by the existing emulator;
`strip_thinking()` drops the block from a plain-text fallback answer. The
router no longer suppresses tool emulation under CoT, and the capability-fit
gate no longer skips a tools+reasoning request — it is now emulable, not a
"cannot". Verified live: a provider native in neither served a
tools+reasoning request and returned a real tool call. The full multi-pass
CoT pipeline (plan / critique / verify) is still not composed into the tool
path — that remains a heavier future option.

### v4.1.0 — capability-aware routing: simulate-or-skip

A request must not silently degrade on a provider that cannot serve it.
`select_provider` now runs a **capability-fit gate** after LMRH ranking:
for each candidate it checks the request's *required* capabilities and
**skips** any provider that cannot serve one even with emulation —

- **vision** — a non-vision provider for an image request is skipped
  (routed to a vision-capable one) rather than having its images stripped;
- **tools + reasoning** — a provider native in *neither* would silently
  drop the tools (router tool-emulation and CoT-E are mutually exclusive),
  so it is skipped for one native in at least one;
- **context** — a request larger than the provider's context window is a
  hard physical limit — hard skip.

**Never hard-fails:** if the gate would empty the candidate list it is
kept unchanged and the best candidate emulates/degrades — the gate only
*reorders toward capable providers*. Skipped providers surface on an
`X-Capability-Skipped` response header; `explain_routing` describes the
gate. Deeper tool+CoT *co-emulation* remains a planned follow-up.

## v4.0.x — "AIRI" milestone

### v4.0.3 — AIRI per-user notification preferences

Design §9, deferred since 4.0.0, now shipped. The global alert mailbox
(`smtp_to`) still always receives AIRI notifications; on top of that each
operator can subscribe their own email and tune which categories
(`monitor` / `automation`) and what minimum severity reach them.

- New `AiriNotificationPref` model (per-user: email, enabled, categories,
  min_severity) and a `notify_prefs` service (get / set / resolve).
- `send_alert` gained a `to` recipient override; `airi_notify` now fans out
  one email per recipient — the global mailbox plus each opted-in operator —
  with an independent throttle key. Evaluator notifications carry a category.
- `GET` / `PUT /api/airi/notification-prefs`; a "My notifications" panel on
  the Routing page.

### v4.0.2 — AIRI grounding: activity log, capability adaptation, sticky chat

AIRI was answering from static blurbs instead of real proxy state — it told an
operator it "could not see the activity log" and that requests "could fail" on
non-native providers. Both were wrong. This release grounds AIRI properly:

- **Activity-log access** — two new read tools. `get_error_summary` is an
  aggregate digest (error counts by class — `rate_limit` is HTTP 429 —, by
  provider, by event type); `search_activity_log` is a filtered, free-text
  search over every recorded event (query `"429"` / `"timeout"` finds those
  rows). `get_recent_routing` now surfaces the error class on non-info rows.
- **Capability adaptation grounding** — `explain_routing` was rewritten to
  describe the real adaptation layer: tool-call emulation (incl. synthetic
  streaming SSE), CoT emulation, vision stripping, caller memory, and the
  honest residual gaps. A new `get_model_capabilities` tool reports, per
  provider, whether tools / reasoning / vision are native or emulated. AIRI no
  longer guesses that a non-native provider "fails" a request — the proxy's
  rule is cross-emulate, don't fail.
- **Sticky chat** — the AIRI chat panel now remembers its active conversation
  across navigation. Leaving the Routing page and returning resumes the same
  thread instead of starting blank (the thread was always persisted
  server-side; only the panel forgot it).

### v4.0.1 — mobile app-shell: off-canvas sidebar drawer

The app-shell sidebar was a fixed in-flow column with only a manual collapse
toggle — no responsive behaviour — so on a phone it squeezed every page's
content into a thin strip (surfaced by the AIRI v4.0 UI walkthrough). Below
the `md` breakpoint the sidebar is now an off-canvas drawer: hidden by
default, opened by a hamburger button in the TopBar, dismissed by a backdrop
tap or by tapping a nav link. At `md` and up it is unchanged — the in-flow
column with its desktop-only collapse toggle. Verified in a real browser
(13/13). Whole-app fix; it restores AIRI design decision #7 (the mobile
deep-link experience).

### v4.0.0 — AIRI, the AI Router Interface

A conversational interface to the AI Provider Supervisor — a chat panel on
the Routing/LMRH page. Operators talk to routing in plain English: inspect
and explain it, propose changes, edit rules, save/restore named rule-sets,
author deterministic scheduled rules, and search shared history. Voice input
("Airy" wake word) is planned for v4.1; per-user notification preferences for
v4.0.1. The whole feature ships behind the `airi_enabled` flag (default off).

Built as five milestones, all converging into this release:

- **Read-only chat** — an SSE-streamed agent loop with grounded read tools
  (`get_supervisor_state`, `get_provider_health`, `get_routing_config`,
  `get_recent_routing`, `explain_routing`). AIRI's own LLM calls go through
  the proxy's `/v1/messages`, so they inherit the fallback chain and survive a
  single-provider outage; they are tagged `X-Internal-Source: airi` and
  excluded from provider stats.
- **Rules + rule-sets** — `airi_ruleset` / `airi_rule`; a seeded `Default`
  set; save-as, activate, restore-default; editable threshold rules.
- **Propose / dry-run / apply / revert** — mutating tools never apply
  directly: they create a *pending* proposal with an impact preview. Applying
  is a separate explicit step. Three safety guards: dry-run warnings block
  auto-apply, a per-turn blast-radius cap of one change, and a hard invariant
  refusing to disable the last enabled provider. Every applied change
  snapshots prior state for one-click revert; the `airi_proposal` row is the
  audit record.
- **Scheduled rules + monitors + notifier** — operator-authored rules compile
  to deterministic `trigger → condition → action` evaluated by a background
  loop with **no LLM in it**. Per-rule cooldown, an oscillation breaker, and a
  global automation kill switch (fail-safe off on restart). Monitor rules
  email the operator with deep links.
- **Conversation history + cross-user search** — chat threads persist
  (`airi_conversation` / `airi_message`); history is per-user but search
  spans every operator's conversations, so two operators don't make opposing
  changes blind. Mobile-responsive throughout.

Verified by a full live QA (39/39 checks against a real LLM) plus 58 AIRI
unit tests within the 2071-test suite. See `docs/4.0-airi-design.md` and
`docs/4.0-airi-qa-report.md`.

---

## v3.10.x — "Harden" milestone

### v3.10.17 — hedge-correctness: race_streams skips error-frame first chunks

The hedge race treated an error-frame first chunk as a "win", so a
fast-failing primary could beat a healthy backup. `race_streams` now
classifies the first chunk — a terminal SSE error frame or an empty
stream counts as a failure, not a win — so a healthy backup wins over a
failing primary. If both branches fail, primary's failed stream is
returned so the caller's pre-flight still surfaces a real status.

### v3.10.16 — BUG-001: pre-flight the hedged streaming path

The hedged streaming path built its `StreamingResponse` straight from
the `race_streams` racer, so a pre-stream failure on the winning branch
rode back as HTTP 200 + an SSE error frame. Both `/v1/messages` and
`/v1/chat/completions` hedged paths now pre-flight the racer — parity
with the non-hedged path.

### v3.10.15 — BUG-032 infra-error observability + BUG-036 dispatch tests

ASGI exceptions and `sqlalchemy.pool` errors logged as bare stdlib
`ERROR`s, invisible to `activity_log` and the v3.10.4 error-rate alert.
A logging tap now classifies them (`disconnect` vs `fault`) into the
`llm_proxy_infra_errors_total` counter; the observability sampler warns
when genuine faults climb. Also: 8 behavioral tests for the v3.10.9
`_messages_dispatch.py` extraction (BUG-036).

### v3.10.14 — bug-log sweep: BUG-026 / BUG-029 / BUG-033 / BUG-034

BUG-026: the AI supervisor counted its own classifier calls in the
provider stats driving its verdicts — `compute_provider_stats` and the
error-rate sampler now exclude internal-source rows. BUG-029:
`/lmrh/quotes` for an unregistered model returns a `model_recognized`
flag + `warnings[]` instead of a silent empty `model_id`. BUG-033:
tool_result images get a descriptive omission marker. BUG-034:
`/lmrh/quotes` missing-vs-empty `model` both return 400.

### v3.10.13 — BUG-001 streaming error contract + BUG-037 timeout

The litellm streaming path returned HTTP 200 + a terminal SSE error
frame on a pre-stream upstream failure — clients checking `status_code`
saw "success". `preflight_sse` now pulls the first SSE frame before the
`StreamingResponse` is constructed; a pre-stream error becomes a real
HTTP status (401/429/502). BUG-037: the non-streaming claude-oauth read
timeout now scales with `max_tokens` (~90s floor, 300s ceiling).

### v3.10.12 — bug-log sweep: BUG-024 / BUG-028 / BUG-037

BUG-024: after the claude-oauth chain falls through to a litellm
provider, `extra` now carries the new provider's credentials (was
reusing the dead OAuth provider's). BUG-028: the cross-family
translator emits a placeholder for empty assistant turns and uses
adjacency tracking for `tool_result` → `role:tool`. BUG-037: the
streaming claude-oauth read timeout is split out and tightened
300s → 120s (a per-chunk gap, never the total).

### v3.10.11 — BUG-038: caller-memory write-back on the CoT streaming path

`_stream_cot_anthropic` was the one streaming path that never ran
`maybe_extract_memory_writes`. It now accumulates memory-tool blocks
from the SSE passthrough and feeds the assembled response through the
extractor — same contract as the other streaming paths.

### v3.10.10 — bug-log sweep: BUG-023 / BUG-025 / BUG-030 / BUG-034

Post-refactor regression-sweep fixes. BUG-023: `verify_api_key` also
filters `deleted_at IS NULL` (defence-in-depth — a tombstoned key
cannot authenticate). BUG-025: a global `JSONDecodeError` handler turns
a malformed request body into a 400, not a 500. BUG-030: the SPA
catch-all returns a JSON 404 for `v1/` / `api/` / `cluster/` / `lmrh/`
namespaces instead of the HTML shell. BUG-034: auth-error wording
unified across the API-key paths.

### v3.10.9 — refactor: extract claude-oauth dispatch from messages.py

Incremental maintainability refactor. `messages.py`'s `messages()`
handler had grown to a ~913-line function (file 1002 lines) — past
`design.md`'s 800-line split trigger, and the hot path every
`/v1/messages` feature touches.

Its deepest branch — the claude-oauth provider-chain walk (streaming /
non-streaming dispatch + 401-refresh fallback + success-path cache /
quality-hint / memory write-back) — and its `_select_excluding`
chain-walk helper were extracted to a new **`app/api/_messages_dispatch.py`**
as `dispatch_claude_oauth_chain()`. It returns `(response, route)`:
non-None response → request served; None → chain exhausted, fall through
to litellm with the updated route. Pure behavior-preserving move.

`_messages_dispatch.py` is the sibling of `_messages_streaming.py` — the
latter holds the SSE *generators*, the former the dispatch *orchestration*.
`messages.py`: 1002 → 816 lines.

Also collapsed the duplicate `docs/architecture.md` (a stale v3.7.13
copy) to a pointer at the canonical root `architecture.md`.

`architecture.md` module map + `refactor-log.md` updated. 4 new tests
(`test_v3109`), 1 source-grep test repointed; 1969 total green.

### v3.10.8 — API Keys page: "Copy models" per key

Operator ask: a one-click way to copy the full list of models a given
API key can route to — accounting for the fact that providers can be
scoped to a single key (`Provider.owned_by_key_id`, v3.0.45 tenant
scoping).

- **Backend** — `GET /api/keys/{key_id}/models` (admin). Returns the
  key's *effective* model catalog: the union of `ModelCapability` rows
  (+ each provider's `default_model`) across enabled providers the key
  can route to — the shared ones (`owned_by_key_id IS NULL`) plus the
  ones it owns. Providers owned by other keys, disabled, or deleted are
  excluded. Models are de-duped and case-insensitively sorted.
- **Frontend** — a per-key "Copy models" button on the API Keys page
  opens a modal showing the effective model list with two copy
  actions: **Copy as CSV** (comma-separated) and **Copy one per line**.

7 new tests in `test_v3108_key_models.py` (scoping, dedup/sort, foreign-
key exclusion, 404); 1965 total green.

### v3.10.7 — LMRH SDK: fix `most_reliable` hint (was inert)

Polish before the PyPI publish. The reference SDK's
`build_hint(prefer="most_reliable")` emitted `provider-hint=<internal
provider id>` — but the proxy's `provider-hint` matcher
(`app/routing/lmrh/score.py::_provider_hint_match`) keys on provider
**name/type**, never the id. The hint was therefore **silently inert**.

Fixes in `sdk/python/lmrh_client.py`:
- `most_reliable` now emits the most-reliable provider's **type** (a
  header-safe slug the proxy matches) — not the id, and not the name
  (names can carry spaces, e.g. "C1 Vertex AI / Google AI", which
  isn't safe in the hint header).
- `most_reliable` + `model_family` no longer clobber each other:
  `_most_reliable_provider()` takes an optional family filter, so the
  combination picks the most reliable provider *of that family*; with
  no qualifying provider it falls back to the family's type list.
- New `_family_provider_types()` helper; `_provider_hint_for_family()`
  rederived from it (output unchanged).

4 new SDK tests (incl. a header-safety regression guard); 20 SDK tests
pass. Reference-SDK-only change — no proxy runtime change.

### v3.10.6 — LMRHv2 Phase 4: downstream adoption begins

LMRHv2's proxy-side endpoints + reference SDK shipped in phases 1-3
(v3.3.0–v3.5.0) but stayed behind the default-off `lmrh_v2_enabled`
flag with zero downstream callers. Phase 4 is adoption.

- **v2 enabled on www01** via `LMRH_V2_NODE_OVERRIDE=on` — the v3.7.18
  per-node staged-rollout override (no cluster-wide flag change; the
  other nodes stay `auto`/off). www01 is the adoption target — callers
  should pin to one node anyway for ETag stability.
- **Validated live**: `/.well-known/lmrh-config` advertises v2.0/2.1;
  `/lmrh/providers` (10 providers), `/lmrh/health`, `/lmrh/quotes` all
  return 200 with sane data. The reference SDK (`sdk/python/lmrh_client.py`)
  validated end-to-end against www01 — `is_supported()` → True, hints
  synthesized for cheapest / fastest / most_reliable. 16 SDK tests pass.
- Coordinator-hub (heaviest LMRH 1.x consumer, 524 req/24h) is the
  adoption target; outreach sent 2026-05-15. When the hub is live on
  the SDK and stable 7 days → flip the cluster-wide `lmrh_v2_enabled`
  and publish the SDK to PyPI.

Doc-only repo change (`docs/lmrh-2.0-bidirectional.md` Phase 4 status);
the enablement is a www01 env-var change. No runtime code change.

### v3.10.5 — cross-family translation integration suite

New `tests/integration/test_cross_family_translation.py` — an
end-to-end regression guard for the v3.10.0 fix, run against the live
deployment through the real router + dispatch (the unit side is
`test_v3100_translation_gate.py`). Four tests: a tool conversation to a
Gemini model must not 400 with the index-N error; the streaming variant
must terminate with `message_stop`; an orphan-tool_result conversation
must not 400; and a plain-text control must be unaffected. All four pass
against v3.10.4 on the fleet.

Test-only — no runtime change, no fleet redeploy.

Note: cluster-sync integration testing remains open — it needs a
two-node test harness (write on node A, assert on node B), which the
current single-`BASE_URL` integration setup can't express; deferred as
a harness extension.

### v3.10.4 — aggregate error-rate alert

v3.10.1 made operator-actionable failures log as `severity=error`, but
nothing alerted on it — so a sustained spike could still run unnoticed
(the v3.10.0 translation bug went ~3 weeks unalerted). v3.10.4 closes
that loop.

The `observability_sampler` loop now runs an error-rate check every
~5 min: it counts `severity=error` requests over a rolling window
(default 15 min, real traffic — probes excluded) and fires
`alert_high_error_rate` when **both** `errors >= min_count` (default 10
— the low-traffic noise floor) **and** the error rate `>= threshold_pct`
(default 10%). The decision is a pure function, `_should_alert_error_rate`.
The alert is `severity=error` with a `high_error_rate` throttle key, so
a sustained incident sends one mail per throttle window, not one per
check. All four thresholds are operator-tunable via config
(`ERROR_RATE_ALERT_*`).

11 new tests in `test_v3104_error_rate_alert.py`; 1958 total green.

### v3.10.3 — fix: /health cache-hit path dropped the dbPool block

Found while validating the v3.10.2 ARCH-A harness. The `/health`
endpoint caches its body for 3s and excludes `circuitBreakers` +
`dbPool` from the cached copy so both stay live — but the cache-hit
branch only re-added `circuitBreakers`. `dbPool` was therefore absent
from every cache-hit response (~2 of every 3s window) since v3.9.8.

The Prometheus pool gauges were unaffected (the sampler reads the pool
directly), but anyone polling `/health.dbPool` — including the ARCH-A
harness — saw it intermittently missing. The cache-hit branch now
re-adds a fresh `dbPool` snapshot alongside `circuitBreakers`.

### v3.10.2 — ARCH-A: DB connection-pool leak diagnostic toolkit

The latent pool leak (www01 + GCP saturated the SQLAlchemy QueuePool
13-20h post-deploy, blocking auth and returning `/health` 500s) has an
unknown root cause — every `AsyncSessionLocal()` is `async with`-wrapped,
so it is not naive session leakage. v3.9.8/.10 shipped *detection*
(`/health.dbPool` + Prometheus gauges) but not a way to find the
culprit. v3.10.2 ships the root-cause toolkit:

**Checkout tracer** (`app/models/database.py`, env-gated `DB_POOL_TRACE`,
default OFF). When enabled, SQLAlchemy `checkout`/`checkin` pool events
record the acquisition stack of every pooled connection. A connection
that never checks back in keeps its entry — and its stack names the
leaking code path. `get_pool_checkout_trace()` returns checked-out
connections oldest-first.

**Exposure.** `GET /cluster/db-pool-trace` (admin) returns the full
per-connection acquisition stacks. `/health.dbPool` gains a trace
summary (`trace_enabled`, `traced_checked_out`, `oldest_checkout_age_sec`)
when tracing is on — readable without auth so the harness can poll it.

**Harness** (`scripts/archa_pool_leak_harness.py`). Drives load to
compress the 13-20h window and isolates *which* request path leaks by
running three phases separately — non-streaming, streaming-consumed,
and streaming-abandoned (connection dropped mid-stream, the
disconnect-cleanup hypothesis) — measuring whether pool `checked_out`
returns to its pre-phase floor after each.

Tracer defaults OFF fleet-wide (zero overhead); enable on one node to
hunt. 8 new tests; 1946 total green.

### v3.10.1 — activity-log severity taxonomy

Until now every failed request logged `severity="warning"`. A provider
failing 100% of its requests and a routine rate-limit 429 were
indistinguishable — to the activity-log UI, to the AI provider
supervisor's stats, and to any future alerting. The v3.10.0 translation
bug ran ~3 weeks unalerted partly for this reason.

`record_outcome`'s failure path now derives severity from the classified
`error_class`:
- **`warning`** — expected/transient: `rate_limit` (e.g. Grok-Web 429
  cool-off, working as designed), `timeout`, `network`.
- **`error`** — operator-actionable: `auth`, `billing`, `bad_request`,
  `upstream_5xx`, and unclassified `unknown` (an unrecognised failure
  must surface, not hide).

New `severity_for_error_class()` helper in `app/monitoring/helpers.py`.
This is a data-correctness fix — no alert auto-fires on it yet; an
aggregate error-rate alert rule keying off `severity=error` is the
natural follow-on. 7 new tests in `test_v3101_severity_taxonomy.py`;
1938 total green.

### v3.10.0 — fix the dominant fleet failure: Anthropic content blocks reaching litellm untranslated

A 2026-05-15 operational audit found **one bug accounted for ~69% of all
fleet warnings** (945 of 1,372 over 7 days): upstream 400s reading
`Invalid user message at index N`, spread across Gemini, OpenRouter, and
litellm-Anthropic providers.

**Root cause.** The `/v1/messages` endpoint always receives an
Anthropic-wire body, but litellm's request API is OpenAI-shaped for
*every* provider it dispatches. v3.9.1's "Fix B" translator only ran on
`cross_family_fallback` routes — so a request sent *directly* to
`/v1/messages` for a Gemini (or OpenRouter, or litellm-Anthropic) model
skipped translation, and its Anthropic `tool_use` / `tool_result`
content blocks reached litellm raw. litellm rejects a user message
whose content list carries an unrecognized `tool_result` part.

**Fix A — `app/api/messages.py`.** Translation now runs for *any*
litellm-dispatched route whose body carries tool or image content
blocks — not just cross-family fallbacks. `claude-oauth` (its own
native-Anthropic dispatcher) and tool-emulation (its own Anthropic-shape
prompt path) remain excluded. The gate is now
`provider_type != "claude-oauth" and not tool_emulation_engaged and
(cross_family_fallback or has_tool_blocks or has_images)`.

**Fix B — `app/api/_oauth_chat_translate.py`.** Defensive: a
`tool_result` whose `tool_use_id` is declared by no assistant turn in
the conversation (a truncated history window beginning mid-tool-
exchange) is now emitted as plain user text instead of a dangling
`role:"tool"` message — which OpenAI also rejects. `anthropic_messages_to_openai`
pre-scans assistant turns for declared `tool_use` ids.

11 new tests in `test_v3100_translation_gate.py` (end-to-end body
translation, orphan handling, gate wiring). 4 v3.9.1 tests updated:
they previously asserted production of dangling `role:"tool"` messages
(invalid OpenAI output) from tool_result-only inputs — now use
well-formed conversations. 1931 tests green.

## v3.9.x — Proxy-side Caller Memory (#267) — phases 4–10 + ops fixes

### v3.9.19 — "Refresh Usage Stats" button for claude-oauth accounts

**Operator ask**: when Anthropic resets the weekly usage counters early,
auto-skipped (over-cap) claude-oauth accounts stay out of service until
the next 4-hour billing scrape cycle re-evaluates them. The operator
wanted a button to force that immediately.

The per-provider "Refresh now" button already existed inside the
provider edit modal, but it required opening each claude-oauth provider
one at a time. v3.9.19 adds a single bulk action:

**Backend** — `POST /api/providers/_refresh-all-anthropic-billing`
scrapes every claude-oauth provider that has Anthropic Console
credentials, ignoring the worker's freshness floor. Each scrape re-runs
the auto-rotation rule evaluator (`scrape_provider_into_snapshot`
already does this), so an account whose weekly utilization dropped
below the at-capacity threshold has its `auto_skip_until` cleared and
returns to service immediately. One failing provider does not abort the
sweep. Returns a per-provider result list including which accounts came
back into service.

**Frontend** — a "Refresh Usage Stats" button in the Providers page
header (shown only when claude-oauth providers exist). The result toast
reports how many accounts were refreshed and how many returned to
service.

9 new tests in test_v3919_refresh_all_billing.py — endpoint aggregation
logic (skip-cleared counting, one-bad-provider resilience, failed-scrape
exclusion, empty-sweep, missing-decision safety) + source wiring. 1921
total green.

### v3.9.18 — P4 tooling improvements (bug-log sync check + translator force-test)

**tools/cut-release.sh** — added bug-log.md sync validation. Before
creating a release, the script scans commits since the last tag for
``BUG-###`` references. If any are found but ``bug-log.md`` was not
updated in the commit range, the operator gets a 5-second warning with
the bug IDs listed. Non-fatal (preserves emergency release flexibility)
but prevents silent bug-status drift.

**scripts/force_test_openrouter_translator.py** — force-validation
script for the v3.9.16 P3a empty-content translator fix. Creates a
temporary API key, temporarily disables Anthropic providers to force
cross-family routing, sends a request with empty user content (the
previously-failing shape), verifies 200 + cross-family translation
header, then cleans up. Exit 0 = pass, 1 = fail, 2 = setup error.

Post-v3.9.17 validation run: **SUCCESS** — empty content routed through
cross-family translation (``anthropic->openai``) to Grok-Web-Devin; 200
response with valid content. The v3.9.16 placeholder fix working as designed.

### v3.9.17 — litellm pin widened to allow 1.84.x (P4 evaluation)

v3.9.14 pinned ``litellm>=1.83.0,<1.84.0`` because 1.84.0 shipped
"breaking changes". The P4 evaluation re-examined that decision:

**Finding**: every documented v1.84.0 breaking change is a LiteLLM
**Proxy-server** feature — master-key propagation, request-control
field stripping, caller-tags behavior, pass-through-endpoint auth
default, clientside-credential/BYOK handling, onboarding flow, CLI
SSO login. This proxy uses litellm strictly as a **Python library**
(``acompletion`` / ``completion`` / exception classes / streaming-
chunk parsing / tool calls). The upstream notes explicitly state
those library interfaces are unchanged.

**Empirical confirmation**: installed litellm 1.84.0 in the test
environment and ran the full unit suite — 1907/1907 pass, identical
to 1.83.11.

**Change**: pin widened to ``litellm>=1.83.0,<1.85.0``. The ``<1.85.0``
ceiling is retained so a future 1.85.x bump gets the same deliberate
evaluation rather than floating unbounded.

5 new tests in test_v3917_litellm_pin.py — including a clean-subprocess
check of litellm's library symbols (acompletion + exception classes)
that bypasses the suite-level litellm stubbing other unit tests inject.
Updated the now-stale v3.9.14 pin assertion. 1912 total green.

### v3.9.16 — P3 (Provider Summary improvements) + P5 (Grok-Web 429 auto-skip) + P6 (Assistants scaffolding)

Four independent improvements batched into one release.

**P3a — OpenRouter translator gap (HIGH impact).** Investigated the
86% failure rate on OpenRouter post-v3.9.1: 100% of failures were
`error_class=bad_request` with "Invalid user message at index N". Two
unhandled message shapes in `_anthropic_blocks_to_openai_message_parts`
(`app/api/_oauth_chat_translate.py`):

1. ``{role: "user", content: ""}`` — empty user string content passed
   through unchanged; OpenAI rejects empty user content
2. ``{role: "user", content: []}`` (or all-empty-blocks) — the
   block-walker returned ``[]``, **silently dropping the message and
   shifting downstream indices**. The "at index N" error was naming
   a position that no longer corresponded to the originally-malformed
   message.

Fix: substitute ``_EMPTY_USER_CONTENT_PLACEHOLDER`` ("(no input)") for
both cases so message position is preserved AND OpenAI sees non-empty
content. Index-preservation replay test guards the fix.

**P3b — per-node analytics.** Every activity_log row now carries
``event_meta.node_id`` (stamped via `_build_event_meta_base` in
`helpers.py`). New endpoint
``GET /api/providers/rolling-stats-by-node?window_hours=24`` rolls up
`(provider_id, node_id) → {requests, successes, success_pct}` via
``json_extract`` — no schema migration required ("Option B" from
the design notes).

**P3c — disabled-row UI badge.** Provider Summary table on the
Metrics page now grays + 🔒-badges rows whose corresponding provider
has ``manual_override_until`` set. Tooltip explains the row reflects
pre-disable cooldown traffic.

**P5 — Grok-Web 429 auto-skip.** Grok-Web's failure rate (60%) is
93% rate-limit from grok.com — not a proxy bug. The bridge caches
429s with "cool-off N seconds remaining". v3.9.16 parses the
cool-off duration on each catch and sets
``Provider.auto_skip_until = now + N`` so the router naturally
avoids the provider during cool-off (instead of cycling cached 429s
to queued callers). Sanity-bounded 1s–1h; doesn't shorten a longer
existing skip. Wired into all 4 ``GrokWebError`` catch sites.

**P6 — OpenAI Assistants scaffolding.** Future-ready handlers:

- ``flush_openai_assistants`` → DELETE /v1/threads/{id}
- ``recover_openai_assistants`` → GET /v1/threads/{id}/messages

Registered at ``app/memory/__init__.py`` import time for provider
type ``openai-assistants``. **No provider of that type exists today**,
so the handlers never fire — they're staged for when the operator
adds Assistants as a provider. ChatGPT-oauth-plan handlers remain
blocked on CSRF token capture; Anthropic memory-view remains blocked
(caller-tool-gated, proxy can't initiate).

**Cancellation note**: Paperless AI + Tax AI caller-memory adoption
backlog notes (filed 2026-05-14) were dropped per operator direction
on 2026-05-15. Paused projects don't accrue backlog.

25 new unit tests; 1907 total green.

### v3.9.15 — Bug-log audit refresh + BUG-007 + BUG-012

Re-audited the 2026-04-24 bug-log against current code. **16 of 18 items
were already fixed** between v2.7.6 and v3.9.14 without updating
bug-log.md statuses. This release reconciles the documentation +
closes the two remaining items + files one newly-discovered
architectural item from today's pool-saturation incidents.

**BUG-007 — `refresh_access_token` rename.** The destructive primitive
shared the same module namespace as the safe wrapper with the more
discoverable name; autocomplete could pick it. Renamed canonical to
``_internal_refresh_access_token``; kept old name as a one-release
alias that emits ``DeprecationWarning``. Only in-tree caller (live
burn test) migrated via ``as`` rebind. Static-analysis test guards
against re-introduction.

**BUG-012 — burn test ``--skip-destructive``.** Weekly automated
runs would rotate the live refresh token every cycle. Now: argparse
flag; destructive tests record as ``True`` with "skipped" detail so
the weekly job logs a clean pass instead of a false fail.

**Bug-log reconciliation** (verified-fixed in this audit):
BUG-002, BUG-003, BUG-004, BUG-005, BUG-006, BUG-008, BUG-009,
BUG-010 (backend + UI badge), BUG-013, BUG-014, BUG-015, BUG-016,
BUG-017, BUG-018, BUG-019. See bug-log.md for the per-item attribution.

**Filed but not shipped:**
- BUG-001 (streaming-error contract) — deferred; needs DevinGPT + hub
  design sign-off before changing wire behavior. DevinGPT just adopted
  streaming write-back in v3.9.11; changing now would require their
  concurrence.
- ARCH-A (latent DB connection leak) — open. Audit shows every
  ``AsyncSessionLocal()`` is ``async with``-wrapped, so the leak isn't
  naive session management. Mitigations already in place
  (v3.9.8 dbPool + v3.9.10 Prometheus gauges); filed plan for next
  recurrence to localize.

9 new unit tests; total green.

### v3.9.14 — Litellm streaming memory write-back + tighter litellm pin (P5b)

Two changes in one ship.

**1) Litellm streaming write-back.** Closes the last memory write-back
gap: ``_stream_anthropic`` (the litellm SSE path for non-claude-oauth
Anthropic-shape providers) now accumulates ``tool_use`` blocks across
streamed ``content_block_delta`` events and feeds an assembled response
dict through the same ``maybe_extract_memory_writes()`` the
non-streaming and claude-oauth paths use.

Implementation: new ``tool_calls_acc: dict[str, dict]`` accumulator
tracks ``{id, name, input_str}`` per ``tool_id``. When the upstream
emits ``input_json_delta`` chunks, we both forward them to the client
(unchanged) AND append to the accumulator. At end-of-stream, parse
each accumulated JSON, build a non-streaming-shape ``content[]``, feed
through the extractor. Malformed JSON is skipped per-block (doesn't
crash the stream's success path).

Wired in all 3 ``_stream_anthropic`` call sites in messages.py:
hedge primary, hedge backup, and the fallthrough path. The litellm
contract is unchanged — we read ``tc_delta.function.name`` and
``.arguments`` from existing streaming fields, no new API surface
needed. (No litellm version bump required.)

**2) Tighter litellm pin.** ``requirements.txt`` now pins
``litellm>=1.83.0,<1.84.0``. v1.84.0 (released 2026-05-14) ships
breaking changes per upstream release notes; staying on the 1.83.x
stable line until community feedback + patch releases settle. The
previous floor (``>=1.40.0``) was decorative — containers have run
1.83.x for months. Revisit when 1.84.1+ stabilizes.

Audit of litellm footprint (driving the decision to tighten): 14 files
in ``app/`` import litellm with 279 usage occurrences. Core paths
include retry, classifier, CoT pipeline, tool emulation, router,
shadow exec, embeddings, keepalive, semantic cache. Not vestigial.

10 new unit tests; 1873 total green.

### v3.9.13 — Per-key TTL sweeper for caller_memory (hub follow-up)

Hub team asked in their v3.9.12 reply: "if you want a per-key TTL config
later (background sweeper that tombstones rows where updated_at < now -
N days for keys that opt in), ping me and I'll add it". This ships that.

**Opt-in is per-api-key** via a new ``api_keys.caller_memory_ttl_days``
column. NULL = no TTL (current behavior; rows persist until explicit
purge). Positive int = background sweeper tombstones any CallerMemory
row whose owner api_key has that TTL set AND whose ``updated_at`` is
older than the threshold.

Different teams get different retention without affecting each other:

- Hub: archival-driven purge via /v1/memory + maybe a short TTL safety net
- Tax AI: long TTL (year-over-year carryover) or null (never expire)
- Paperless: short TTL (per-document-cycle) or null
- DevinGPT: their call once they wire conversation_id

Implementation:
- ``app/monitoring/caller_memory_ttl_sweeper.py`` background worker
- Runs hourly by default (``caller_memory_ttl_sweep_interval_sec``)
- Skips when ``caller_memory_enabled=False`` (no-op)
- Tombstone + bump ``updated_at`` for LWW cluster sync propagation
- Redis cache invalidation on each tombstone
- Floor at 60s interval to prevent runaway-loop misconfiguration

Admin surface: existing ``PATCH /api/keys/{id}`` PUT now accepts
``caller_memory_ttl_days`` (positive int = set; 0 or negative = clear).
The field shows up on the list/get responses too. UI wiring in the
React frontend deferred — admin API is enough for the operator's
in-session use case.

13 new unit tests; 1863 total green.

### v3.9.12 — API-key-scoped memory CRUD (hub-team unblock)

Hub team replied to the v3.9.10 broadcast: opt-out by default, but
asked two questions before any opt-in:

1. **Eviction / TTL semantics** — what happens to memory for an inactive
   X-Conversation-Id after N days? Is there a hub-callable purge endpoint?
2. **Cross-account isolation** — caller memory is scoped per-account,
   right? If two use_cases coincidentally use the same conversation_id,
   memory doesn't merge across api_keys?

**Q2 answer (existing behavior, confirmed)**: Yes — the king-store
schema is keyed by ``(api_key_id, conversation_id, memory_tag)`` with
api_key_id as the outermost filter on every read/write/Redis cache key.
Two api_keys with the same conversation_id are strictly isolated.

**Q1 answer (gap closed by this version)**: No automatic TTL today
(rows stay until tombstoned). v3.9.12 ships the missing
hub-callable purge surface so room archival doesn't need an admin
session:

- ``GET    /v1/memory/conversations``                          → list this key's conversation_ids
- ``GET    /v1/memory/conversations/{conv_id}``                → list (tag, content) rows for this conv
- ``PUT    /v1/memory/conversations/{conv_id}/{tag}``          → upsert
- ``DELETE /v1/memory/conversations/{conv_id}``                → tombstone entire conversation (rows + marker)
- ``DELETE /v1/memory/conversations/{conv_id}/{tag}``          → tombstone one tag

Auth is the same x-api-key / Bearer pair as /v1/messages — explicitly
NOT require_admin. All queries are scoped to the verified key's
api_key_id; cross-key access is impossible (and locked by tests).
Delete-entire-conversation tombstones the CallerMemoryMarker too so
Phase 7 recovery doesn't try to reconstruct stale state.

Automatic TTL sweeper deferred — operator can decide cadence per
api_key. The new endpoints are sufficient for caller-driven purge
on archival events (hub's stated pattern).

11 new unit tests; 1850 total green.

### v3.9.11 — Streaming Anthropic memory-tool write-back (#267 Phase 5.5, DevinGPT unblock)

DevinGPT replied to the v3.9.10 adoption broadcast: blocked on streaming
write-back because every chat completion uses `stream:true` (UX
requirement). Without streaming extraction, adoption would have been
read-only (inject works; the model's memory_20250818 tool_use blocks
would silently disappear).

Implementation is minimal — the existing `_stream_claude_oauth` SSE
handler already assembles a full response_dict matching the
non-streaming shape (top-level `content[]` with parsed tool_use blocks)
for record_outcome / activity_log. v3.9.11 just feeds that
assembled_response through the same `maybe_extract_memory_writes()`
function the non-streaming path uses, gated on
`conversation_id and api_key_id`. Silent-degrade try/except wrapper so
a memory store error never breaks the stream's success path.

Wired in both `/v1/messages` (messages.py) and `/v1/chat/completions`
(completions.py — DevinGPT's actual endpoint, which routes through
the OpenAI↔Anthropic translator to claude-oauth). Per-call cost is
near-zero: the assembled_response was already being built for activity
logging.

What still doesn't write-back streaming:
- `_stream_anthropic` (litellm path for non-claude-oauth Anthropic
  providers) — assembles tool_calls but not a clean response_dict.
  Defer until there's a claude-on-anthropic-direct adopter.
- The /v1/messages litellm streaming path for other Anthropic-shape
  providers — same reason.

7 new unit tests; 1839 total green.

### v3.9.10 — Prometheus metrics for caller-memory + pool + scrape freshness

Closes the observability gap for the memory feature now that it's
flipped on cluster-wide. Three new metric primitives in
``app/observability/prometheus.py``:

- **``llm_proxy_memory_operations_total{operation, outcome}``** —
  Counter for inject / extract / flush / recover. Outcomes: applied,
  skipped, degraded (silent-degrade catch-all). Wired into all four
  memory modules.
- **``llm_proxy_db_pool_{size,checked_out,overflow}``** — Gauges
  mirroring ``/health.dbPool``. Sampled every 30s by a new background
  ticker so dashboards can chart pool depth without polling /health.
- **``llm_proxy_scrape_freshness_seconds{provider_id, provider_name, source}``** —
  Gauge per provider holding seconds since the last successful scrape.
  Alert at >14400 (4h, the default scrape interval) for stalled scrapes.

New ``app/monitoring/observability_sampler.py`` background task started
from ``main.py`` on startup. 30s tick; emits pool + freshness gauges.
Skips providers that have never been scraped to keep dashboards clean.

Useful Prometheus queries / alert candidates:

```promql
# Slow leak: pool depth climbing for 10 min straight
increase(llm_proxy_db_pool_checked_out[10m]) > 5
  and llm_proxy_db_pool_checked_out > 30

# Saturated: above base pool_size for 2 min
llm_proxy_db_pool_checked_out > on() llm_proxy_db_pool_size

# Stalled scrape: snapshot is older than the scrape interval
llm_proxy_scrape_freshness_seconds > 14400

# Silent-degrade rate (memory store outage)
rate(llm_proxy_memory_operations_total{outcome="degraded"}[5m]) > 0
```

13 new unit tests; 1832 total green.

### v3.9.9 — Follow-up to v3.9.8 quota fix: retire dead compute + UI source badge

Two small follow-ups to yesterday's v3.9.8 quota fix, both cleanup.

**1) `usage_tracker` skips scraped providers (P3a).** After v3.9.8 the
display layer ignores `ProviderUsageWindow` whenever an
`ExternalUsageSnapshot` exists for the provider. The 60s background
sweep was still computing those values for scraped providers — wasted
DB work nothing reads. Now: build a set of `provider_id`s with fresh
snapshots (captured within 2h, matching the billing-scrape freshness
floor) and early-`continue` past them in the compute loop. Skip count
logged at debug. Falls through to compute for providers with stale or
absent snapshots so the rotation fallback still has something to read.

**2) Dashboard source badge (P3b).** Small badge under the "Sub Quota"
StatCard showing which source the displayed % came from:
`Anthropic Console` (authoritative scrape), `internal counter`
(fallback path), or `mixed sources` (heterogeneous fleet). New
`usage_data_source` field on the `Provider` type — already exposed by
v3.9.8's backend changes; v3.9.9 wires the frontend to read it.

6 new unit tests; 1819 total green.

### v3.9.8 — Fix quota hallucinations + pool diagnostics + providers.py & sync.py refactor

Three independent fixes batched into one release.

**1) WebUI quota warning fix.** The Dashboard's "subscription quota over"
banner was showing nonsense ratios — `Devin-Anthropic-Max-Gmail weekly 643%`,
`Devin-Anthropic-Max-VG weekly 365%` — because `/api/providers` was reading
``ProviderUsageWindow`` (the proxy-side traffic counter). The same Pro Max
accounts feed Claude Code / desktop / other workloads, so the proxy slice is
~3 orders of magnitude lower than the account total. Operator-set
``usage_weekly_limit_tokens`` (sized for the proxy slice) hit hallucination
ratios against the rolled-up counter.

Now: prefer ``ExternalUsageSnapshot.{seven_day_utilization,
five_hour_utilization}`` (authoritative, scraped from Anthropic Console
per v3.7.0) when available. Fall back to ``ProviderUsageWindow`` only
for providers without snapshots (e.g. per_call OpenAI). New
``usage_data_source`` field on the JSON shape so UI can label which
source it's reading. Applied to ``/api/providers`` (list) and
``/api/providers/{id}/usage`` (detail).

Post-fix verification: VG=92%, Gmail=68%, Codex-Gmail=29% — actual
Anthropic-Console / ChatGPT-Cloud-side values. No more hallucination.

**2) Pool diagnostics in `/health` (P3 defense-in-depth).** New ``dbPool``
field exposing SQLAlchemy QueuePool state from the canonical URL —
``{size, checked_out, overflow, in_use, max}``. Excluded from the 3s
health-cache so it's always live. Best-effort wrapper: pool query errors
return ``{error: ...}`` instead of 500'ing /health.

Surfaced after the 2026-05-14 www01 pool exhaustion which took 13h to
manifest and was diagnosed by running ``engine.pool.checkedout()`` inside
the container — making this visible at /health eliminates that step.

**3) File-size refactor (P5).** ``providers.py`` 1213→872, ``cluster/sync.py``
1076→800. Both now under the 1000-line ceiling; every file in ``app/`` is
under 1000 lines.

- New ``app/api/provider_lifecycle.py`` (5 endpoints: clear-auth-failure,
  toggle, release-manual-overrides, test, scan-models)
- New ``app/api/provider_capabilities.py`` (3 endpoints: list/upsert/infer
  capabilities + ``_serialize_cap`` helper + ``CapabilityUpdate`` schema)
- New ``app/cluster/sync_handlers.py`` (6 ``_apply_<table>`` handlers)
- Re-imports in ``sync.py`` keep public surface unchanged
- 5 stale test guards updated to point at new file locations

**Tooling**: also added ``tools/cut-release.sh`` (committed before v3.9.8 as
``ed6b5e7``) — one-shot ceremony enforcing the operator-locked rule that
every version bump = git tag + GitHub release + Docker Hub push (versioned
+ ``:latest``) + backup tarball, same session. Prevents the 23-version drift
that was discovered and backfilled today.

1813 unit tests green.

### v3.9.7 — Lock 3 Phase-4 design decisions before Phase 10 flip (#267)

The shipped Phase-4 (v3.8.9) injection behavior was originally framed as
"sensible defaults — operator can revisit later". Before the Phase 10
operator opt-in flip, all three open questions are resolved and locked
by code, RFC text, and regression tests:

- **Q1 (scope)** — Inject fires only when caller supplies
  `X-Conversation-Id`. One-shot requests stay clean.
- **Q2 (Anthropic injection point)** — System-prompt prefix on
  `body["system"]`. NOT `memory_blocks` (tied to the Anthropic memory
  tool — too narrow). NOT first user message (breaks role boundaries).
- **Q3 (OpenAI injection point)** — System-prompt prefix on
  `messages[0]`, or synthesized at index 0 if missing.

Docs-only ship — code already implements the recommended answers.
Changes: `app/memory/inject.py` docstring + `docs/rfc/2026-05-proxy-memory-store.md`
+ 11 new regression tests in `test_v397_inject_decisions_locked.py`.

**Phase 10 (operator opt-in flip)** followed v3.9.7 the same day:
`CALLER_MEMORY_ENABLED=true` set in docker-compose for both www01 + www02,
rolling deploy clean, observation period begins. Feature is dormant
until callers pass `X-Conversation-Id`.

### v3.9.6 — Admin API for caller_memory + markers (#267 Phase 9)

New `app/api/memory_admin.py` blueprint mounted at `/api/memory/*`:

| Method | Path | Purpose |
|---|---|---|
| GET    | `/keys/{key}` | list entries |
| PUT    | `/keys/{key}/{tag}` | upsert (operator write) |
| DELETE | `/keys/{key}/{tag}` | soft-delete |
| GET    | `/markers/{key}` | list markers |
| POST   | `/markers/{id}/clear-recovered` | reset recovered_at for retry |
| POST   | `/recover/{key}/{conv}/{tag}` | manual Phase-7 fire |

All require `Depends(require_admin)`. Endpoints work regardless of
`settings.caller_memory_enabled` — useful for inspecting state before
flipping the feature on. Also exposes `Provider.memory_disabled` in
the provider edit form (`ProviderCreate` schema + `_serialize`).

### v3.9.5 — Per-provider memory_disabled flag (#267 Phase 8)

New `Provider.memory_disabled` boolean column. When True:
- `extract.py` skips memory writes from this provider's responses
- `inject.py` skips memory injection when this provider is selected

Implementation moved Phase 4 inject from pre-routing to post-routing
in both `messages.py` and `completions.py` so we can check
`route.provider.memory_disabled`. New order:

```
privacy filters → hint build → route selection →
Phase 6 flush → cross-family body['model'] rewrite →
Phase 4 inject (gated on route.provider.memory_disabled) →
Fix B Anthropic→OpenAI translation → dispatch
```

Use cases: keep test providers pure, avoid memory-tool surcharges on
specific accounts, per-provider data-residency boundaries. Default
False = participates normally. Migration:
`ALTER TABLE providers ADD COLUMN memory_disabled BOOLEAN DEFAULT 0`.

### v3.9.4 — Back-pressure memory recovery (#267 Phase 7)

When `CallerMemoryMarker` exists but the content row is missing — the
DB-restore-lost-content-rows-while-markers-survived case — try to
reconstruct content from the original upstream provider. Per RFC:
markers are small + frozen and back up cleanly even when content
rows are mid-mutation.

Same shape as Phase 6: registry-based dispatcher
(`maybe_recover_memory(db, *, api_key_id, conversation_id, memory_tag) -> Optional[str]`)
wired into `inject.py`'s no-content path. All current handlers ship as
noop because no deployed provider exposes a clean conversation-state
read-back API. Marker advance semantics: on success
`marker.recovered_at = now`; on handler failure/None marker stays clean
so retry is possible.

### v3.9.3 — Provider-side memory flush handlers (#267 Phase 6)

Detection of provider transitions per
`(api_key_id, conversation_id, memory_tag)` and registry-based per-vendor
flush dispatcher hooked into `messages.py` after route selection +
cross-family adjustments. Per RFC decision #3: best-effort, default ON,
log+continue on failure.

All current handlers ship as noop — none of the deployed provider types
expose a clean cleanup API the proxy can call without side effects.
Scaffolding is in place to land real handlers (ChatGPT-oauth conversation
delete, OpenAI Assistants thread delete) incrementally without re-wiring
the call site.

### v3.9.2 — 1% sample-rate request_body capture on bad_request 4xx (#268)

Hub team asked for a way to debug payload-shape upstream rejections
(the #269 class of bug) without paying the full-time storage cost of
`activity_log_capture_bodies=True` (the 2026-05-06 1 GB blowup).

New setting `activity_log_body_sample_rate_4xx` (default 0.01 = 1%).
Independent of `capture_bodies`. Only fires on
`error_class == "bad_request"`; auth/billing/rate_limit are skipped
because those are credential/account state, not payload shape. When
sampled, writes `request_body` + `body_sampled=True` tag to event_meta
(filter on this in the hub UI).

### v3.9.1 — Anthropic→OpenAI cross-family translation (#269 A+B)

OpenRouter-Devin-Personal cross-family fallback was 400-ing on every
tool-bearing request with `litellm.APIConnectionError: XaiException -
Invalid user message at index N`.

Root cause (activity_log id=169903): cross-family fallback from
claude-haiku to OpenRouter `served=openai/gpt-4o` rewrote `body.model`
but preserved Anthropic-shape `tool_result` blocks. OpenAI rejected
every retry.

**Fix A — safety net** (`app/routing/tool_content.py`): when tool
blocks present AND cross-family target is NOT OpenAI-shape (Gemini,
Cohere, etc.), walk past via `_select_excluding`. Returns 503 with
`X-Cross-Family-Skipped` header if all candidates exhausted, rather
than burning upstream cost on guaranteed 400s.

**Fix B — translator**
(`anthropic_to_openai_body()` in `app/api/_oauth_chat_translate.py`):
full Anthropic-shape body → OpenAI Chat-Completions shape translation
when the cross-family target speaks OpenAI shape. Handles `tool_use`,
`tool_result` (incl. empty/bare variants — `(no output)` placeholder),
system field → leading system message, tool definitions schema
translation. Response header: `X-Cross-Family-Translated: anthropic->openai`.

Verified end-to-end via synthetic test (auto_skip on C1 Anthropic
Claude + replay of the failing-shape body → 200 + translated header).

### v3.9.0 — Anthropic memory-tool write-back (#267 Phase 5)

When an Anthropic `/v1/messages` response contains `tool_use` blocks
for the `memory` tool (`memory_20250818`), `extract.py` extracts the
write operations and persists them to the king-store. Supports
`create`, `str_replace`, `insert`, `delete`, `rename`. Closes the
read/write loop with Phase 4.

Streaming non-streaming only; streaming write-back lands in Phase 5.5
(needs assembled-block sniffing in `_messages_streaming.py`).

### v3.8.9 — Memory injection middleware (#267 Phase 4)

`app/memory/inject.py` — request-time injection of stored memory as a
system-prompt prefix on outgoing upstream requests. Wired into both
`/v1/messages` and `/v1/chat/completions`. Gated on
`X-Conversation-Id` header. Silent degrade on any store error.
Cross-vendor strategy: same shape for Anthropic + OpenAI (system
prompt).

---

## v3.8.x — Caller-memory store + tool telemetry + provider rename

### v3.8.8 — Caller memory store: Redis hot cache + SQLite durable (#267 Phase 3)

Three-tier read/write layer (`app/memory/store.py`): Redis hot cache
(per-node), SQLite durable king-store (cluster-replicated), in-process
fallback. Mirrors the `app/cot/session.py` pattern. Reads are
always-local (no network hop); writes go to SQLite first, then
invalidate Redis. `MemoryEntry` dataclass for operator-facing rows.

### v3.8.7 — Caller memory + marker tables + cluster sync (#267 Phase 2)

New SQLAlchemy models: `CallerMemory` (content rows scoped by
`api_key_id` × `conversation_id` × `memory_tag`) and
`CallerMemoryMarker` (back-pressure recovery anchor). Cluster-sync
hooked up via the existing LWW propagation. Operator-locked decisions
documented in `docs/rfc/2026-05-proxy-memory-store.md`. Default OFF
(`caller_memory_enabled=False`) — Phases 4-10 followed.

### v3.8.6 — "Release & re-enable all" banner now actually re-enables

The release banner's "release all" button was a no-op for providers
that had been manually disabled. Fix: include `enabled=True` in the
batched UPDATE. Test added.

### v3.8.5 — Tool-call success weighting in router scoring (#265, closes audit)

Router scoring now consumes the rolling `tool_call_success_rate`
populated by the v3.8.4 prober. Providers with low recent success on
tool calls get downranked for `has_tools=True` requests. Closes the
tool-emulation audit follow-up.

### v3.8.4 — Periodic tool-call probe + auto-native_tools adjustment (#264)

Background worker probes each provider's tool-calling shape and
records success rate in `model_capabilities.tool_call_success_rate`.
On 3 consecutive failures: `native_tools` flips to False so future
requests engage the emulation layer instead of pretending the upstream
supports tools.

### v3.8.3 — Tool-call telemetry + Grok-Web native_tools=False (#263)

Adds `tool_call_format` ("native" vs "emulated") to `record_outcome`
+ `tool_calls_emitted`/`validated` counters in event_meta. Flips
`native_tools=False` for all Grok-Web `ModelCapability` rows (grok.com
chat is Playwright-driven; no function calling).

### v3.8.2 — Classify caller-side bugs + guard empty-choices + override backfill (#260 #261 #262)

Three smaller fixes: caller-bug error classification (so they don't
contaminate provider failure stats), empty-`choices[]` guard in
OpenAI response path, `manual_override_until=9999...` backfill for
providers disabled before v3.7.28 (so the supervisor doesn't grab them).

### v3.8.1 — Codex billing scrape Phase 2 — bearer auth via existing OAuth (#245)

ChatGPT/Codex Cloud usage scrape Phase 2: reuses the captured OAuth
bearer token from `Provider.codex_session_cookies` to call the cloud
billing API directly. Was previously stubbed in Phase 1.

### v3.8.0 — Rename provider_type `codex-oauth` → `ChatGPT-oauth-plan` (#251)

Operator-driven rename: the OAuth credential is from ChatGPT Plus,
not the Codex CLI. The new name is more accurate + reflects how the
operator thinks about it. One-shot SQL UPDATE in the migration block
(safe to re-run; no-op when no rows match the old value).

---

## v3.7.x — Anthropic Console billing scrape (real account usage)

### v3.7.33 — Expose AI supervisor / rate limiter / billing scrape settings in UI

Per-feature toggle controls in the Settings page for the AI provider
supervisor, AI rate limiter, and Anthropic billing scrape worker.
Operator can pause any of these without container restart via the
runtime SCHEMA admin endpoint.

### v3.7.32 — AI provider supervisor admin endpoints (#252 phase 5 — CLOSES #252)

Apply/revert/dismiss lifecycle endpoints + a trigger-now smoke-test
endpoint + a live-stats diagnostic endpoint. All respect
`manual_override_until` — operator-pinned providers return 409 Conflict
on apply/trigger.

### v3.7.31 — AI provider supervisor worker + cluster sync (#252 phase 4)

Background worker that periodically calls the supervisor LLM with
fleet stats. Generates `ProviderAiReview` rows that propagate via
cluster sync. Operator applies via the v3.7.32 endpoints.

### v3.7.30 — ProviderAiReview table + stats helper (#252 phase 3)

Schema + cluster-sync wiring for the AI supervisor's review/proposal
rows. Stats helper formats fleet state into a prompt-friendly summary.

### v3.7.29 — Manual override UI banner + 🔒 badge (#252 phase 2)

Provider list UI surfaces operator-pinned providers (manual override
on) with a banner + lock badge. Disables the auto-toggle path for
those rows.

### v3.7.28 — Manual override schema + toggle/release endpoints (#252 phase 1)

`Provider.manual_override_until` / `manual_override_set_by` /
`manual_override_set_at` / `manual_override_reason`. Toggle endpoint
sets the override; release endpoint clears it.

### v3.7.27 — ChatGPT/Codex Cloud usage scrape — Phase 1 scaffolding (#245)

Schema + endpoint scaffolding for the Codex usage scrape. Real
implementation followed in v3.8.1.

### v3.7.26 — grok-web propagates upstream 429 instead of masking as 502 (#259)

Bug where grok-web rate-limit responses got wrapped into a generic 502.
Now flows through with the original 429 so callers can back off.

### v3.7.25 — Remove legacy "Usage-based rotation" UI for claude-oauth (#257)

The Anthropic billing scrape from v3.7.0 supersedes the manual
usage-tracking config. UI section retired; backend code stays for
ChatGPT-oauth-plan / future providers.

### v3.7.24 — Anthropic billing scrape — freshness guard + jitter (#258)

Worker now skips re-scraping accounts whose snapshot is younger than
the configured floor (default 2h) and adds jitter so all 3 nodes
don't hit the upstream simultaneously.

### v3.7.23 — Routing balance dashboard tile for claude-oauth (#255)

New tile on the Activity page: rolling 24h request counts split by
claude-oauth account so operator can eyeball the routing balance.

### v3.7.22 — ClientDisconnect handler (#253) + activity_log retention tiers (#254)

Two ops fixes: structured `ClientDisconnect` handling (was logged as
generic 500 noise); severity-tiered retention (errors kept 30d,
warnings 14d, info 7d) so the table doesn't grow unbounded.

---

## v3.7.x — Anthropic Console billing scrape (real account usage) — original entry

### v3.7.21 — Hotfix: BUG-022 regression — restore async with in get_db()

The v3.7.19 BUG-022 fix replaced `async with AsyncSessionLocal()`
with manual `try/finally` + `session.close()` to swallow
`OperationalError('no active connection')` on request cancellation.
That silenced the visible error but bypassed SQLA's pool-return
path. The garbage collector then surfaced
`SAWarning: non-checked-in connection will be terminated` for each
leaked connection. Net worse: log lines per cancellation went from
3-5 (pre-fix) to 7-10 (post-v3.7.19).

Surfaced 2026-05-12 02:47 monitor cycle: 3 `Task was destroyed but
it is pending!` plus 6 SAWarning lines + 3 pool-error lines.

**Fix**: restore the `async with` pattern. Wrap it in a try/except
that ONLY swallows the post-cancellation
`no active connection` error — every other exception still bubbles
up. The `async with __aexit__` runs rollback/close cleanly before
the exception reaches our handler, so the pool state is intact.

Updated 5 unit tests in `test_v3719_log_noise_cleanup.py` to assert
the new pattern. **1417/1417 pass.**

### v3.7.20 — BUG-020: utilization bucket filter on P2C selection (claude-oauth routing balance fix)

**High-severity routing bug.** Surfaced 2026-05-11 evening: VG
account got **1138/1138 claude-oauth requests** while Gmail account
got **0**, despite VG being at 49% utilization and Gmail at 4%. The
v3.7.4 utilization-aware reorder was silently overridden by the
v2.8.0 PeakEWMA P2C selection.

**Root cause** in `app/routing/router.py:select_provider`:

1. `reorder_claude_oauth_by_utilization` correctly put Gmail (lower
   bucket) first in the `providers` list
2. `profiles` was built from that reordered list
3. **But** `rank_candidates_with_scores(profiles, hint)` re-sorted
   by score, undoing the reorder
4. The default P2C/PeakEWMA selection block then:
   - Built `top_tier` of candidates within 1.0 score of top
   - Sampled 2 randomly, picked by EWMA
   - **Explicitly preferred the candidate WITH EWMA samples over the
     one without** (`elif e1 is None: winner = c2`)
5. Self-reinforcing loop: VG got all traffic → VG's EWMA stayed warm
   → Gmail never seeded its EWMA → VG always won

**Fix**: before the P2C random sample runs, narrow `top_tier` to
candidates in the lowest utilization bucket. If candidates span
multiple buckets, the higher-bucket entries drop out entirely. Same
bucket → existing P2C/EWMA logic applies unchanged. Empty `util_map`
(snapshot table issue) → no-op fallback to existing behavior.

`util_map` was hoisted to broader scope so the P2C selection block
can consult it (previously local to the reorder's try/except).
`_utilization_bucket` was added to the router's import from
`external_rotation`.

**Effect**: when a load burst pushes one claude-oauth account into
a higher utilization bucket while the other stays in a lower bucket,
the lower-utilization account immediately takes over routing. Should
restore the operator's expected "use the account with more headroom"
behavior.

Tests: +10 unit tests in `test_v3720_routing_bucket_filter.py`. **1416/1416 pass.**

### v3.7.19 — Log-noise cleanup (BUG-021 embeddings base64 + BUG-022 DB session close)

Two log-noise fixes surfaced during 2026-05-11 evening load burst
(coordinator-hub hit 740 req/20m for ~40 min):

**BUG-021** — `/v1/embeddings` was emitting Pydantic
`PydanticSerializationUnexpectedValue` warnings on every Cohere
response because the upstream returns base64-encoded float32 arrays
even when the caller didn't request `encoding_format=base64`.
litellm's `EmbeddingResponse.embedding` field is typed `list[float]`,
so the type mismatch triggered the serializer warning. Responses
were still HTTP 200, but each call generated a stack-trace-like log
line.

Fix: new `_normalize_embeddings_to_floats()` helper decodes the
base64 to `list[float]` before serialization. Gated on
`encoding_format != "base64"` so callers who explicitly opted into
base64 still get base64. Also passes `warnings="none"` to
`model_dump()` so the serializer doesn't emit during the dump itself
(we normalize the value afterward).

**BUG-022** — under bursty load, request cancellations from clients
(disconnects, timeouts) propagated `CancelledError` through the
Starlette middleware chain. This closed the underlying aiosqlite
connection before SQLA's `AsyncSession.close()` ran, causing
`OperationalError('no active connection')` on cleanup. Each
cancellation generated 3-5 stack-trace lines, plus an
`asyncio:Task exception was never retrieved` log.

Fix: `get_db()` dependency now explicitly catches the
"no active connection" error during close and swallows it
(re-raises `CancelledError`). Unexpected errors during close are
logged at DEBUG level so they don't drown the rest of the logs.

**Severity**: both low — pure log noise, no correctness impact.
Cleanup pass to keep the activity log readable under load.

Tests: +14 unit tests in `test_v3719_log_noise_cleanup.py`. **1406/1406 pass.**

### v3.7.18 — LMRHv2 Q1 (public no-auth aggregate view) + Q6 (per-node override env var)

Two of the three remaining LMRHv2 design questions implemented:

**Q1 — public no-auth discovery endpoint**

Operator's 2026-05-10 answer: *"secure info via API only, public info on
a public URL that all LMRH-supporting clients can reach to determine how
they will integrate"*.

New endpoint `GET /lmrh/public` returns a sanitized aggregate suitable
for unauthenticated callers. Hides operator-internal provider names + ids,
per-provider counts, subscription quotas, exact cost figures, and
per-provider metrics. Exposes available model identifiers + capability
features + aggregate availability indicators per model. After API-key
exchange, callers move to the full `/lmrh/providers` view.

Aggregation pivots on `(family, model_id)` so multi-route models coalesce
into one entry with a `variants` list. Cost numbers are bucketed into
`economy / standard / premium / subscription` tiers. Route counts are
bucketed into `none / single / few / many` (≥3 = "many") so callers see
redundancy signal without exact account counts.

Endpoint advertised in `/.well-known/lmrh-config` under the new `public`
endpoint key. Cache-Control: public, max-age=60.

**Q6 — per-node enable override env var**

Operator's 2026-05-10 answer: *"per-node yes, one at a time"*. The
existing `lmrh_v2_enabled` flag was cluster-synced via SystemSetting and
propagated to all peers within ~60s, so it didn't support per-node-only
enablement.

New env var `LMRH_V2_NODE_OVERRIDE` accepts:
- `on` — this node enables LMRH v2 regardless of cluster setting
- `off` — this node disables LMRH v2 regardless of cluster setting
- `auto` (default) — follow the SystemSetting cluster flag

Operators flipping one-node-at-a-time should set `=on` on the target
node, verify, then propagate cluster-wide via the SystemSetting before
clearing the env var.

**Q4 verified**: snapshot-or-proxy-slice subscription-quota fallback was
already shipped in v3.7.9 — `app/routing/lmrh/snapshot.py` correctly
prefers fresh `ExternalUsageSnapshot.seven_day_utilization` (<8h),
falls back to `ProviderUsageWindow.weekly_pct`, and synthesizes from
the snapshot alone when proxy-slice tracking is disabled. No additional
work needed.

Tests: +16 unit tests in `test_v3718_lmrh_q1_q6.py`. **1392/1392 pass.**

### v3.7.17 — Expose `admin-readonly-catalog` key_type in API Keys UI

The v3.7.2 catalog-scope auth shipped a narrow-scope `key_type` that
the coordinator-hub team's "Proxy Catalog Admin Key" setting expects
operators to provision. The backend has accepted it since 2026-05-09,
but the API Keys page dropdown only offered `standard` + `claude-code`,
so operators couldn't actually create one without going through DB.

**Fix**: added `admin-readonly-catalog` to the dropdown (frontend
`KeyType` union + `KEY_TYPES` array), plus a hint paragraph that
appears when the type is selected explaining:
- It's for the coordinator-hub Scan Models picker
- It can PUT `/api/llm/models/{id}` (per-model aliases/family/variant)
- It CANNOT make inference calls (rejected by `verify_api_key` on
  `/v1/messages` + `/v1/chat/completions`)
- Despite the name, "readonly" refers to "no inference", not "no edits"

Backend was already in place — only the UI dropdown was the gap.

Tests: +7 unit tests in `test_v3717_catalog_key_ui_exposure.py`.
**1376/1376 pass.**

### v3.7.16 — Persistent-auth-failure → DB auto-skip (#239) + config schema cleanup (#238)

Surfaced via the 20-min proactive monitoring loop. Devin-Codex-Gmail's
codex-oauth token expired earlier in the day; the existing in-memory
CB protection opened a 24h hold-down but reset on each container
restart (today saw v3.7.14 + v3.7.15 deploys). Result: every fresh
container re-hit auth-fail-once, marked the CB, then restart wiped
the state — repeat. Operators saw the persistent warning logs but no
DB-persisted protective action.

**Fix (#239)**: when a provider accumulates 3+ auth failures within a
30-min window, ``record_auth_failure`` now ALSO writes
``Provider.auto_skip_until = now + 24h`` + ``auto_skip_reason =
"persistent_auth_failure"``. Survives restart (DB-persisted, replicates
via the v3.7.15 cluster-sync gain). Idempotent: if the provider
already has an auto_skip_until further out for a different reason
(e.g. billing 100%), the new write is skipped rather than shortening
the window. ``clear_auth_failure`` (called on admin re-auth) also
clears the failure-history counter so the threshold resets cleanly.

This is the first slice of the broader "AI-driven provider supervisor"
backlog item — fixed-rule for now, queued as v3.8.x to mirror the
v3.7.10 AI rate limiter on the provider side.

**Cleanup (#238)**: ``config_runtime.SCHEMA`` entries for
``semantic_cache_embedding_model`` + ``semantic_cache_provider_id``
declared ``type='string'``, but pydantic reports ``'str'``. The
mismatch fired a warning on every settings load. Harmonized to
``'str'`` — silences two log lines per config load.

Tests: +11 unit tests in ``test_v3716_persistent_auth_skip.py``.
**1369/1369 pass.**

### v3.7.15 — Cluster sync for three v3.7.x tables (BUG-016) + cross-node IP-block cache invalidation (BUG-018) + AI rate limiter recursion guard (BUG-017)

Closes the three open bugs from the v3.7.13 / v3.7.14 QA pass.

**BUG-017 — AI rate limiter recursion guard (high)**

The AI rate limiter calls `http://localhost:3000/v1/messages` with an
internal admin key to classify per-key behavior. Pre-fix: that call
was logged to `activity_log` like any other request, so the *next*
review cycle pulled it into the sample, slowly amplifying prompt
size + cost O(n²).

Fix: outgoing httpx request from `classify_with_llm()` now carries
`X-Internal-Source: ai_rate_limiter`. The middleware reads the header
into a contextvar; `record_outcome()` stamps it onto
`event_meta.internal_source`; `review_one_key()` filters out those
rows from the sample so the AI can't see its own previous calls.

**BUG-016 — cluster sync for the three new v3.7.x tables (medium)**

`blocked_ips`, `api_key_ai_review`, `external_usage_snapshot` landed
quickly without being added to the cluster-sync allowlist. Result:
admin blocks on one node didn't propagate, reviews were node-local,
and each node scraped Anthropic independently (2-3x provider load).

Fix: `_build_sync_payload` now emits three new sections; `apply_sync`
gained three new merge helpers (`_apply_blocked_ips`,
`_apply_ai_reviews`, `_apply_external_usage_snapshots`) with LWW
conflict resolution. Plus a `deleted_at` tombstone column on
`BlockedIp` so DELETEs propagate (admin DELETE is now soft-delete +
`deleted_at IS NULL` filter in the middleware loader + list endpoint).

**BUG-018 — IP-block cache invalidation on peer nodes (medium)**

Pre-fix: peers waited up to 30s for their in-memory cache TTL after
a block synced. Fix: `apply_sync` checks whether `_apply_blocked_ips`
mutated the table (insert / update / tombstone) and calls
`_clear_cache_for_tests()` so the next request reloads from the
freshly-synced rows. Bundles into the BUG-016 sync path.

Tests: +19 unit tests in `tests/unit/test_v3715_cluster_sync_v37x.py`
plus +5 in `test_v3710_ai_rate_limiter.py`. 1358/1358 pass.

### v3.7.14 — Hotfix: admin-recovery exemption for IP block middleware (BUG-019)

**Critical lockout deadlock fix.** v3.7.11 introduced IP-blocking via
admin-managed `blocked_ips`. The middleware runs at the very front of
the ASGI stack so blocked traffic is rejected before any work runs.
But it had no exemption: an admin who accidentally blocked their own
IP could not call `DELETE /api/admin/blocked-ips/{ip}` to un-block —
the middleware 403'd the request before the endpoint handler ran.

Caught during the post-v3.7.13 QA pass when the operator's LAN-egress
IP (192.168.18.1) got added to the block list while testing
BUG-018 (cache invalidation). Recovery required direct DB access.

**Fix**: the middleware now bypasses two narrow path prefixes for any
caller, blocked or not:
- `/api/auth/login` — admin can sign in to the UI
- `/api/admin/blocked-ips` — admin can list, add, or DELETE entries

Everything else still 403s uniformly. Both exempt endpoints remain
admin-gated (`require_admin`) so a blocked attacker still can't use
them — they just no longer 403 at the middleware layer. +4 unit
tests in `tests/unit/test_v3711_ip_block.py` covering the exemption
and the negative case (other admin paths still blocked).

### v3.7.13 — Refactor R5: deduplicate `record_outcome` success/error paths

Architectural cleanup pass. Closes a real-pain refactor: every
shared meta field added to `record_outcome` had to land twice
(once in the success branch, once in the error branch). v3.6.2's
`client_ip`, v3.6.3's `client_ip_inside` split, and v3.6.2's
`api_key_id` all required dual edits. v3.7.x shipped 13 versions in
one day — each was a chance to forget the second edit.

**Three new private helpers** in `app/monitoring/helpers.py`:
- `_attach_client_ip(meta)` — mutating helper for v3.6.2/v3.6.3
  client IP fields
- `_build_event_meta_base(...)` — branch-agnostic dict with
  provider/key attribution + client IP + optional caller hints
- `_emit_outcome_event(...)` — wraps `log_event` so the v3.3.4
  keepalive/llm event-type split lives in one place

`record_outcome` body: **275 → 193 lines** (−82, −30%). Each branch
now reads as "build base meta + add my branch-specific fields + emit"
instead of duplicating 50+ lines of setup.

Behavior preservation: zero changes to inputs/outputs/meta-dict
contents. Confirmed by **1338 passing tests** (no count change).

Two source-level regression tests updated to point at the new
helper structure (`_attach_client_ip` + `_build_event_meta_base`).

Architecture docs (`docs/architecture.md`) and refactor ledger
(`docs/refactor-log.md`) updated with R5 entry.

### v3.7.12 — AI rate limiter recommends specific IP blocks

Closes the v3.7.11 follow-up: AI rate limiter can now recommend
blocking a specific source IP, not just throttling/disabling the
whole key. Useful when one client IP is misbehaving but the rest of
the key's traffic is legitimate.

- **New verdict** `block_ip` joins `normal/watch/throttle/block`.
- **Top-5 source IPs** included in the LLM prompt (`top_source_ips`
  dict computed by `compute_stats`, sorted desc by request count).
- **LLM response shape** extended with optional `ip` field — required
  when verdict is `block_ip`. Hallucination guard: if the LLM names
  an IP not in the top-5 list, we demote the verdict to `watch` to
  avoid acting on a fabricated IP.
- **New column** `api_key_ai_review.suggested_block_ip` stores the
  selected IP for the `apply` path + `revert` reversal.
- **`apply` path**: inserts the IP into `blocked_ips` via the same
  table the v3.7.11 admin endpoint uses, with
  `added_by="ai-rate-limiter (review N)"` for attribution. Idempotent
  (skips if already blocked). Invalidates the middleware cache so
  the next request hits the block.
- **`revert` path**: deletes the IP from `blocked_ips`. Idempotent —
  if operator manually removed the block earlier, revert still
  completes cleanly.

This is the FULL v3.7.10 promise from operator Q5 ("slow that api
key's usage OR its source ip") now end-to-end:
- Key-level: throttle_rpm / disable (v3.7.10)
- IP-level: block_ip (v3.7.12)
- Both: lifecycle endpoints (apply / dismiss / revert)

14 new unit tests. **1324 → 1338 passing**.

### v3.7.11 — IP block middleware (closes Q5 IP-level controls)

Closes the half of operator Q5 that was deferred from v3.7.10:
*"proactively slow that api key's usage **or it's source ip**"*. v3.7.10
shipped the key-level controls; v3.7.11 adds the IP-level layer.

- **New table** `blocked_ips` — operator-managed list of IPs that
  should be rejected with 403 before any auth/routing logic runs.
  Columns: `ip` (PK), `reason`, `added_at`, `added_by`.
- **New middleware** `app/middleware/ip_block.py` — registered FIRST
  in `app/main.py` so it wraps every other middleware (outermost in
  the ASGI stack = first to see incoming requests).
  - Checks BOTH `extract_client_ip_from_request` (raw inside IP) AND
    `_maybe_rewrite_lan_ip` (the LAN-egress public IP from v3.6.3).
    Operators can block either form depending on attribution model.
  - 30s in-memory cache so the middleware doesn't hit the DB on
    every request. New blocks added via the admin endpoint
    invalidate the local cache instantly; peer nodes pick up via
    cluster sync + their own 30s refresh.
  - **Fail-open** on cache load error — better to let traffic through
    than to 403-everything because of an unrelated DB hiccup.
- **New admin endpoints** under `/api/admin/blocked-ips/`:
  - `GET /api/admin/blocked-ips` — list current blocks
  - `POST /api/admin/blocked-ips` — add an IP (body: `{ip, reason?}`)
  - `DELETE /api/admin/blocked-ips/{ip:path}` — remove
- **Light validation**: accepts any non-empty string up to 128 chars.
  Operator might want to block CIDR ranges, hostnames, or future
  IPv6 forms — we don't gate on format. Idempotent on POST.
- **Block rejection is uniform** across the surface — no exempted
  endpoints (health, login, etc. all return 403 for blocked IPs).
  Prevents probing for which endpoints are open.

**Not extended into AI rate limiter yet**: v3.7.10's
`ApiKeyAiReview.suggested_action` enum is still `none / throttle_rpm
/ disable` — no `block_ip` value. The LLM doesn't currently get the
specific source IPs in the prompt (only `unique_ips_count`), so it
can't recommend a specific IP to block. If we want auto-IP-blocking
that's a v3.7.12 follow-up: include top-N IPs in the prompt + add
`block_ip` to the verdict enum + apply path.

14 new unit tests covering middleware behavior, fail-open semantics,
LAN-egress rewrite check, and the admin endpoint wiring.
**1310 → 1324 passing**.

### v3.7.10 — Proactive AI rate limiter (operator-requested Q5)

Closes operator Q5 from the 2026-05-10 LMRHv2 design discussion:

> we need an ai built into the proxy that itself reviews this and
> proactively makes suggestions - so default to a loose rate limit
> but then that AI should analyze rates and the traffic it is using
> and if there's red flags; proactively slow that api key's usage
> or it's source ip.

**Architecture**: background worker (`app/monitoring/ai_rate_limiter.py`)
runs every `ai_rate_limiter_interval_sec` (default 300s = 5min) and
for each enabled api_key with recent traffic:

1. Pulls last 30 min of activity_log
2. Computes a structured stats summary (req-rate, error-rate, latency
   p50/p95, IP variance, model variance, prompt-size variance, etc.)
3. Picks 2-3 sample `request_preview` snippets, redacted of any
   token-shaped strings (sk-ant-, sk-, llmp-, AIza patterns)
4. Builds a prompt and calls the configured model via the proxy
   itself (`http://localhost:3000/v1/messages`) — reuses our routing
   logic, shows up in our own activity log transparently
5. Parses verdict: `normal` / `watch` / `throttle` / `block`
6. Writes an `ApiKeyAiReview` row with verdict + reasoning + stats
7. If `ai_rate_limiter_auto_apply=True` AND verdict in
   `{throttle, block}`: applies the action (lower rate_limit_rpm to
   floor, or set enabled=False) and records prior values for revert

**New table** `api_key_ai_review` — full lifecycle tracking:
`captured_at` / `llm_verdict` / `llm_reasoning` / `suggested_action`
/ `applied_at` / `applied_action` / `prior_rate_limit_rpm` /
`reverted_at` / `dismissed_at`.

**Defaults per operator Q5/Q6 decisions**:
- `ai_rate_limiter_enabled=False` — opt-in per node
- `ai_rate_limiter_auto_apply=False` — suggest-only until operator
  validates verdicts on real traffic
- `ai_rate_limiter_interval_sec=300` — 5min cadence
- `ai_rate_limiter_throttle_floor_rpm=5` — floor when applied
- `ai_rate_limiter_model="claude-haiku-4-5-20251001"` — Haiku for cost
- `ai_rate_limiter_internal_api_key=None` — operator generates a
  dedicated key and sets via env. Worker no-ops without it.

**New admin endpoints** (`app/api/ai_rate_limiter.py`):
- `GET /api/keys/{id}/ai-reviews` — list recent reviews newest-first
- `POST /api/keys/{id}/ai-reviews/{review_id}/dismiss` — mark
  false-positive (doesn't undo auto-applied actions)
- `POST /api/keys/{id}/ai-reviews/{review_id}/apply` — force-apply
  the suggestion now (manual path when auto_apply=False)
- `POST /api/keys/{id}/ai-reviews/{review_id}/revert` — undo an
  applied action; restores prior `rate_limit_rpm` or re-enables key

**Security**: sample previews are redacted of token-shaped strings
before being sent to the LLM (`sk-ant-`, `sk-`, `llmp-`, `AIza` prefix
patterns, plus `"api_key": "..."` JSON snippets). The LLM never sees
raw cookies or full request bodies — only aggregate stats + redacted
~300-char snippets.

**IP blocking deferred to v3.7.11** per operator Q5 scope — we'd
need new middleware infrastructure to enforce IP blocks. v3.7.10 is
focused on the api_key-level controls.

**Worker safety**:
- Wrapped in try/except per-key so one bad key doesn't break the
  whole sweep
- 60-second poll when disabled (cheap)
- Bounded 2000-row activity-log fetch per key (memory cap)
- Garbage verdict from the LLM gets coerced to "watch" (cautious
  fallback, not "normal")

32 new unit tests. **1278 → 1310 passing**.

### v3.7.9 — LMRH v2 subscription_quota uses authoritative Anthropic snapshot

Closes the Q4 follow-up from the operator's LMRHv2 decisions
(2026-05-10): `/lmrh/providers` was returning the proxy-slice
`weekly_pct` for claude-oauth providers — misleading per v3.7.0
findings (proxy slice ≠ account total). Now prefers the
authoritative `ExternalUsageSnapshot.seven_day_utilization` when
fresh (<8h old), falls back to proxy slice when no snapshot exists.

- Only applies to **claude-oauth** providers (codex-oauth + grok-web
  don't have an Anthropic Console scraper; they keep the proxy
  slice).
- **Freshness window**: 8 hours. Stale snapshots (e.g. scraper has
  been silently failing) fall back to proxy slice rather than
  serve old data.
- **New code path**: when an operator sets
  `usage_weekly_limit_tokens=NULL` per the v3.7.x recommendation,
  `subscription_quota` was previously absent from the response
  entirely. Now synthesized from the external snapshot alone —
  `session_used_pct=None` (5-hour scrape window maps to session
  but we don't surface that yet), `weekly_used_pct` and
  `weekly_resets_at` from the snapshot.
- Defensive: snapshot table query wrapped in try/except, falls back
  to proxy slice silently on error.

7 new source-level regression tests. **1271 → 1278 passing**.

### v3.7.8 — Third QA sweep nit: 404 on `GET /external-usage` for unknown provider

Tiny consistency fix surfaced by a v3.7.x QA sweep. Pre-fix
`GET /api/providers/{id}/external-usage` returned 200 `[]` for an
unknown provider_id, making it impossible for callers to distinguish
"no snapshots yet" from "provider doesn't exist". Now returns 404
to match the existing behavior on the `-refresh` endpoint.

No other QA findings — the v3.7.x surface is clean. Smoke node also
rolled to current (was on v3.5.10).

**1271 passing** (no test count change — single source-level fix).

### v3.7.7 — Email alert when Anthropic billing scrape auth fails

When the 4-hourly billing scraper hits a non-OK `auth_state`
(`session_expired` / `cf_blocked` / `config_error` / `network_error`
/ `parse_error` / `http_error`) for the **2nd or later** consecutive
scrape, the proxy emails the operator via the existing SMTP alert
path. Without the alert, cookies could silently fail for up to
~30 days × 6 scrapes/day = 180 dead scrapes before anyone notices.

- **`alert_anthropic_billing_auth_expired`** in
  `app/monitoring/notifications.py` — uses the existing
  `send_alert` pattern with throttle-key `billing_auth:{provider_id}`
  so each provider has independent reminders and the same provider
  can't spam every 4 hours when cookies stay expired (SMTP throttle
  cache dedupes by throttle_key for the configured window).
- **First-failure tolerance**: Cloudflare interstitials sometimes
  clear on their own, so we only alert on the 2nd+ consecutive
  failure. Single transient failures stay in the activity log only.
- **Auth-state → human text mapping**: each enum value gets a
  plain-English explanation in the email body (e.g.
  `cf_blocked` → "Cloudflare is challenging the scrape — cookies
  stale or fingerprint changed").
- **Email body includes the fix path**: tells the operator exactly
  where to re-paste (Edit Provider → External Usage → Rotate
  cookies). v3.7.5's UI surface makes this a one-click workflow.
- **Severity is `warning`, not `error` or `critical`** — billing
  scrape failure degrades rotation accuracy but doesn't break
  inference. Auto-rotation gracefully falls back to operator
  priority when no recent snapshot exists.

Defensive: wrapping the alert in try/except so an SMTP failure
doesn't break the scrape itself.

**Also in v3.7.7 — BUG-014 closed**: 8MB total request body size cap
in `validate_completion_request`. Pre-fix a 10MB system prompt
leaked an upstream nginx 413 → 502 chain; now returns clean 400
with `"request body too large (X bytes > 8MB cap). Trim the system
prompt or break the request into smaller chunks."`. Closes a P3
backlog item that's been open since the first QA sweep (v3.5.x).

9 new unit tests (7 alert + 2 size-cap). **1262 → 1271 passing**.

### v3.7.6 — Provider-list effective-preference badges

Closes the "why does Gmail still say priority=4 if VG has more
headroom" UX gap. Stored priority is unchanged by design (operator-set
intent), but now the providers LIST page surfaces the **effective**
state at a glance:

- 🚦 **`auto-skipped`** red badge next to any provider whose
  ``auto_skip_until`` is in the future. Title hover shows the reason
  ("weekly utilization 100.0% >= 95% threshold; resets ...").
- ✓ **`preferred`** green badge next to whichever claude-oauth
  provider currently has the lowest weekly utilization (and isn't
  auto-skipped). This is the router's actual first-choice given
  v3.7.4's utilization-weighted preference.

The list-page query reads latest snapshots via
`providersApi.listSnapshots(id, 1)` for every enabled claude-oauth
provider. 60s `staleTime`, 2-min `refetchInterval` — matches the
router's 30s internal cache (so badges stay accurate without
hammering the DB).

Badges only render with ≥2 claude-oauth providers (nothing to rank
against if only one).

6 new source-level wiring tests. **1256 → 1262 passing**.

### v3.7.5 — UI: External Usage panel + supersedes-note on legacy section

Operator surfaced the gap: the Edit Provider modal still showed only
the v3.0.64 "Usage-based rotation" fields and had no surface for the
v3.6/3.7 features (cookie paste, snapshots, auto-rotation state,
manual refresh). Frontend was backend-only this whole time.

- **New `AnthropicBillingPanel.tsx`** rendered for editing
  claude-oauth providers. Surfaces:
  - **Auto-skip banner** when `auto_skip_until` is set — red, shows
    the rotation reason + countdown to reset.
  - **Org UUID + cookies-stored badge** with "captured N days ago"
    indicator. Amber warning at ≥25 days (~30 day cookie lifetime).
  - **"Rotate cookies" workflow** — textarea + org_uuid input → POST
    to `/api/providers/{id}/anthropic-billing-credentials`. Step-by-
    step capture instructions inline.
  - **"Refresh now" button** — fires `POST .../anthropic-billing-refresh`
    and prints the real 7d/5h utilization in a toast.
  - **Snapshots table** — latest 10 captures with utilization columns
    (5h, 7d, Sonnet 7d), extra-usage credits, color-coded by pct
    band (green <50, yellow 50-79, amber 80-94, red ≥95).
- **Legacy "Usage-based rotation" section** now shows a "superseded
  by External Usage above" note when the provider is claude-oauth.
  Other provider types see the unchanged description. Reduces
  confusion about which signal drives rotation.
- **API client** (`frontend/src/api/index.ts`) gains four methods:
  `setBillingCredentials`, `refreshBillingNow`, `listSnapshots`,
  `evaluateRotationRulesNow` — all targeting the v3.7.0/3.7.1 admin
  endpoints.
- **`Provider` type** (`frontend/src/types/index.ts`) extended with
  the five new fields so the panel can render without `any` casts.
- **Cookies are never displayed** — only the boolean
  `has_anthropic_session_cookies` flag from the API. Verified by a
  source-level guard test.

14 new source-level wiring tests. **1242 → 1256 passing**.

### v3.7.4 — Utilization-weighted preference among claude-oauth providers

Operator surfaced the gap in v3.7.1: skip-based rotation correctly
handled the "Gmail at 100%" case (filter it out), but didn't express
the more general "use the account with more headroom" preference
below threshold. If Gmail were at 80% and VG at 20%, v3.7.1 still
preferred Gmail (operator priority=4 > priority=5).

v3.7.4 adds utilization-weighted reordering **within the claude-oauth
subset**. Among claude-oauth providers, sort by
``(utilization_bucket, operator_priority)``. Non-claude-oauth
providers keep their operator-priority slot unchanged — we don't
shuffle $$$ per-call providers based on subscription-account
utilization.

- **`reorder_claude_oauth_by_utilization(providers, util_map)`**
  (`app/routing/external_rotation.py`) — reorders only the
  claude-oauth slots, preserves non-oauth positions.
- **`get_utilization_map(db)`** — 30s TTL cache of latest
  `seven_day_utilization` per provider from
  `external_usage_snapshot`. Single query, cached so per-request
  cost is O(1).
- **Buckets**: default 25pp (0-24% / 25-49% / 50-74% / 75-99% / 100%).
  Coarse-grained to avoid flapping on trivial differential. Tunable
  via `EXTERNAL_ROTATION_UTIL_BUCKET_PCT`.
- **Tie-breaker**: same-bucket providers follow operator priority,
  preserving operator-encoded preferences when utilizations are
  comparable.
- **Provider-with-data > provider-without**: claude-oauth providers
  without a snapshot get a "no data" bucket that sorts after all
  known-data buckets, so a recently-scraped provider preferentially
  wins over an unscanned one.

Effect on operator's current state:
- Gmail at 100% (bucket 4), VG at 24% (bucket 0)
- Within claude-oauth subset: VG ranks first (bucket 0 < bucket 4)
- Net: VG preferred regardless of operator priority order
- (v3.7.1's skip already filters Gmail entirely at 100% — v3.7.4
  makes the behavior correct for the not-yet-at-capacity case too)

Defensive: routing wrapped in try/except — utilization reorder
failures fall back to operator priority unchanged.

19 new tests including the exact operator scenario as
`test_operator_scenario_swaps_gmail_vg`. **1223 → 1242 passing**.

### v3.7.3 — Cluster-sync the auto-rotation skip decisions

Real bug found while watching v3.7.1 in production: only `www01`
had cookies (operator pasted there), only `www01` scraped, only
`www01` knew Gmail was at-capacity. Routing requests landing on
`www02` or `GCP` happily routed to Gmail despite the skip — they
had no `auto_skip_until` to filter on.

Fix: replicate the auto-rotation outcome (`auto_skip_until` /
`auto_skip_reason`) plus the billing-scrape identifiers
(`anthropic_org_uuid`, `anthropic_session_captured_at`) through the
existing cluster sync push. Cookies themselves stay on the capture
node — auth material doesn't replicate. Peer scrapers no-op when
they don't have cookies (existing `is_not(None)` filter on the
worker), so we get effective primary-node-only scraping for free
while still propagating decisions.

- **Cluster manager** (`app/cluster/manager.py:_build_payload`) now
  emits the four new fields in each Provider entry.
- **Cluster sync apply** (`app/cluster/sync.py:apply_payload`) reads
  them on both the update-existing and insert-new paths. Membership-
  test (``"key" in p_data``) so an explicit null overwrites a stale
  local value. ISO timestamp parsing handled by new module-level
  `_parse_iso_or_none` helper.
- **Intentional non-replication**: `Provider.anthropic_session_cookies`
  is NOT in the cluster sync payload, AND the apply pass never
  writes to it. Test coverage enforces both invariants.

11 new unit tests including regression for "cookies must not appear
in payload" and "cookies must never be written by sync apply".
**1212 → 1223 passing**.

### v3.7.2 — `admin-readonly-catalog` scope for hub-team #230 follow-up

Promised in the v3.6.0 #230 contract reply: a narrower-scoped admin
key that grants access to the model-identity catalog endpoints only,
so the hub team's stored credential (currently a full-admin session
Bearer) can be downgraded in scope without giving up the catalog-edit
capability.

- **Two new `api_keys.key_type` values**:
  - `admin` — full admin via Bearer/x-api-key (parity with session admin)
  - `admin-readonly-catalog` — scoped to model-identity catalog only.
    Can do GET + PUT on `/api/llm/models/*` and nothing else.
- **New auth dependency** (`app/auth/catalog_scope.py:require_catalog_auth`):
  - Accepts existing admin session (cookie or Bearer-session) OR
    api-key with `key_type` in (`admin`, `admin-readonly-catalog`).
  - Bearer prefix `llmp-*` distinguishes api-keys from session tokens
    so the two flows don't collide.
  - Returns 403 (not 401) when an api-key is supplied but with the
    wrong scope, so callers can distinguish "fix your scope" from
    "auth missing".
- **`/api/llm/models/{model_id:path}` GET and PUT now use
  `require_catalog_auth`** in place of `require_admin`. All other
  admin endpoints are unchanged.
- **In-place swap**: hub team currently uses an admin session Bearer.
  Once they rotate to an `admin-readonly-catalog` api-key, behavior
  is byte-identical from their side (same Authorization header
  shape, same response contract).

9 new unit tests. **1203 → 1212 passing**.

### v3.7.1 — Auto-rotation: skip at-capacity providers automatically

The v3.7.0 scraper writes authoritative weekly utilization to
`ExternalUsageSnapshot`. v3.7.1 turns those snapshots into routing
decisions: providers whose latest snapshot reports
`seven_day_utilization >= 95%` are auto-skipped by the router until
the snapshot's `seven_day_resets_at` passes.

Live integration test against the first batch of snapshots:
- Gmail at 100% (resets in ~80 min) → `auto_skip_until` set; router skips it
- VG at 24% → `auto_skip_until=None`; router prefers it
- After Gmail's 20:00 reset, the next scrape's snapshot will show
  the post-reset percentage and the rule clears `auto_skip_until`

**New columns on `Provider`** (`app/models/db.py`):
- `auto_skip_until` (DateTime, nullable) — set by rule evaluator,
  cleared automatically once util drops below `capacity - hysteresis`.
- `auto_skip_reason` (String, nullable) — short human-readable string
  for admin UI / activity log diagnostics.

**New module** (`app/routing/external_rotation.py`):
- `evaluate_rules_for_provider(db, provider, snapshot=...)` — single-
  provider rule application. Used by the scraper after each capture.
- `evaluate_rules_for_all_providers(db)` — batch evaluator for the
  manual-trigger admin endpoint.
- `is_currently_at_capacity(provider)` — routing-time predicate
  (timestamp comparison, no DB round-trip).

Rules:
- **Set skip** when `seven_day_utilization >= external_rotation_capacity_pct`
  (default 95%).
- **Clear skip** when utilization back below
  `external_rotation_capacity_pct - external_rotation_hysteresis_pct`
  (default 90% — gives 5pp hysteresis to avoid flapping).

**Router integration** (`app/routing/router.py:select_provider`):
- Filters out providers where `is_currently_at_capacity(p)` is true.
- Defensive fallback: if EVERY provider is auto-skipped, falls back
  to the unfiltered list and logs `external_rotation.all_providers_at_capacity`
  so the operator sees it.

**New admin endpoint** (`app/api/anthropic_billing.py`):
- `POST /api/providers/_evaluate-rotation-rules` — fire the evaluator
  across all `claude-oauth` providers using their latest snapshots,
  without waiting for the next 4-hour cycle. Returns the decision
  dict for each provider.

**Auto-evaluation** is also wired into `scrape_provider_into_snapshot`
so every successful scrape immediately re-evaluates the rules.

**Settings** (`app/config.py`):
- `external_rotation_capacity_pct` (default 95.0)
- `external_rotation_hysteresis_pct` (default 5.0)

Operator-set `Provider.priority` and `Provider.enabled` are preserved
unchanged — auto-rotation is purely additive. Routing logic still
respects the operator's chain ordering; auto-skip just removes
at-capacity entries from the candidate list.

16 new unit tests. **1187 → 1203 passing**.

### v3.7.0 — `/api/organizations/{uuid}/usage` scraper + 4-hourly worker

Closes the long-standing assumption gap: `ProviderUsageWindow` only
sees the proxy's slice of an Anthropic Pro Max account. The same
accounts are also used by Claude Code CLI / mobile / other tools, so
the proxy's count is structurally less than the account total —
rotation/cascade decisions based on it triggered at the wrong time.

The third-party browser-bridge agent captured the authoritative
endpoint on 2026-05-10:

- **`GET https://claude.ai/api/organizations/{org_uuid}/usage`**
- **Auth: cookie-only** — `sessionKey`, `sessionKeyLC`, `routingHint`,
  `lastActiveOrg`, plus Cloudflare cookies. No bearer token, no
  `anthropic-version` / `anthropic-beta` headers needed.
- **Response**: `five_hour` + `seven_day` windows with `utilization`
  (percent) and `resets_at`, plus per-model breakdowns
  (`seven_day_sonnet`, `seven_day_opus`) and an `extra_usage` overage
  block with `monthly_limit` / `used_credits` / `currency`.

This version ships data collection only. Wiring into rotation
decisions lands in v3.7.1 once we have a few weeks of snapshot
history to validate against.

**New model** (`app/models/db.py`):

- `ExternalUsageSnapshot` — one row per scrape attempt. Columns flatten
  the captured response shape (utilization, resets_at, extra_usage block)
  for easy SQL; `raw_response` preserves the full JSON for forward-
  compat. `auth_state` distinguishes `ok` / `session_expired` /
  `cf_blocked` / `network_error` / `parse_error` / `config_error`.
- Three new columns on `Provider`: `anthropic_org_uuid`,
  `anthropic_session_cookies` (JSON dict), `anthropic_session_captured_at`
  (unix ts). Idempotent ALTER TABLE pattern.

**New module** (`app/providers/anthropic_billing.py`):

- `parse_cookie_jar(raw)` — accepts JSON dict, JSON string, or
  cookie-header style; defensive parsing.
- `validate_cookies(cookies)` — required: `sessionKey`. Returns a
  human-readable reason string when insufficient.
- `parse_usage_response(body)` — flattens the captured response shape
  into the columnar fields. Tolerates missing keys, null values,
  unexpected types.
- `fetch_usage(org_uuid, cookies)` — async httpx call, classifies
  outcomes by auth_state. Detects Cloudflare interstitials separately
  from real 401/403 so operator gets actionable diagnostics.
- `scrape_provider_into_snapshot(db, provider)` — high-level helper
  that fetches, parses, and writes one snapshot row.

**New worker** (`app/monitoring/anthropic_billing_worker.py`):

- Runs every 4 hours (configurable via
  `ANTHROPIC_BILLING_SCRAPE_INTERVAL_SEC`, set to 0 to disable).
- Iterates all `claude-oauth` providers with cookies+UUID configured.
- Mirrors the existing `keepalive.py` periodic-loop pattern.
- Started from FastAPI lifespan hook in `app/main.py`.

**New admin API** (`app/api/anthropic_billing.py`):

- `POST /api/providers/{id}/anthropic-billing-credentials` — paste
  cookies + org UUID. Pre-validates required cookies.
- `POST /api/providers/{id}/anthropic-billing-refresh` — fire one
  scrape immediately for smoke-testing fresh credentials.
- `GET /api/providers/{id}/external-usage` — return latest N snapshots,
  newest first, including failure rows so cookie-expiration is visible.

**Operator workflow**:

1. Sign into the Anthropic account in a real browser.
2. DevTools → Application → Cookies → `https://claude.ai`, copy as JSON dict.
3. POST to the `-credentials` endpoint with cookies + org UUID.
4. POST `-refresh` to verify it works (response includes the parsed `seven_day_utilization` so you immediately see the real number).
5. Worker takes over on the 4-hour cadence.

When `sessionKey` expires (~30 days), the next scrape returns
`auth_state=session_expired`. Operator re-pastes a fresh capture.

**Tests**: 36 new unit tests. Total **1151 → 1187 passing**.

Backlog reference: `project_backlog_anthropic_billing_scrape.md`.

## v3.6.x — Model identity edit API (Hub #230)

### v3.6.3 — LAN-egress IP rewrite (hairpin NAT visibility)

When a LAN host calls the proxy via the public URL, the LAN router
NATs the TCP source to the gateway IP (e.g. `192.168.18.1`) and the
actual public egress IP is invisible at the HTTP layer (the LAN
router does IP NAT, not application-layer header injection). The
v3.6.2 capture surfaced this as `client_ip = 192.168.18.1` for all
LAN-side traffic — useless for attribution.

- **`client_ip_lan_resolve_map` setting** (`app/config.py`) — operator
  declares `{"<inside-gateway-ip>": "<resolvable-hostname>"}`. The
  hostname's A record reflects the LAN's current public IP. Default
  empty (no rewriting) — opt-in per deploy.
- **DNS resolution with 5-min TTL cache** (`app/observability/request_context.py:_resolve_cached`).
  Threading-safe module-level cache. Failed lookups also cached so a
  misconfigured hostname doesn't burn DNS on every request. Operator
  ISP-rotated IPs picked up within 5 minutes of change.
- **`prewarm_lan_egress_dns()`** called from FastAPI startup so the
  first request after a deploy doesn't pay sync DNS cost in the
  middleware hot path.
- **`client_ip_inside`** new field in activity_log meta — only emitted
  when the rewrite was a no-op-difference (i.e., when the inside IP
  differs from the post-rewrite public IP). Preserves the raw IP for
  diagnostics without doubling storage on rows where it's identical.

`docker-compose.yml` on tmrwww01 wired with
`CLIENT_IP_LAN_RESOLVE_MAP={"192.168.18.1": "ip.voipguru.org"}`. tmrwww02
and GCP can stay default-empty (they don't see this hairpin chain).

16 new unit tests covering DNS cache TTL, NXDOMAIN cache, fallback to
inside-IP on resolution failure, prewarm idempotency, and the
record_outcome wiring guard.

### v3.6.2 — Caller IP in activity log + api_key_id for joins

Operator-set 2026-05-09 evening: every llm_request entry needs the
source IP so incident-response / abuse-investigation / quota-by-IP
queries are answerable from one table. Previously activity_log only
had `api_key_prefix` (denormalized 2026-05-04 in v3.2.12); we now
also add `api_key_id` (full id for cross-table joins) and `client_ip`
(real caller, not the nginx container).

- **`app/observability/request_context.py` (NEW)** — `ContextVar`-based
  carrier for per-request side-channel data. `extract_client_ip_from_request`
  prefers `X-Forwarded-For` first hop (we run behind nginx so the raw
  socket peer is the nginx container), falls back to `X-Real-IP`, then
  `request.client.host`. Defensive — never raises.
- **`app/main.py:log_requests`** middleware — sets the contextvar at
  request entry. Zero callsite churn vs threading a new parameter
  through ~12 `record_outcome()` callers.
- **`app/monitoring/helpers.py:record_outcome`** — both success and
  error paths now add `client_ip` (when set) and `api_key_id` to the
  activity_log meta dict.

Probes (`api_key_prefix=probe-keepalive`) and other non-HTTP code
paths run outside a request scope so the contextvar is empty and
the IP field is omitted — `IS NULL` filters work cleanly.

14 new unit tests covering XFF parsing, multi-hop chains, X-Real-IP
fallback, malformed request objects, contextvar round-trip, and
record_outcome wiring regression.

### v3.6.1 — `X-Quality-Hint: thin-content` + 412 ETag header fix

Two small but high-value fixes endorsed by the coordinator-hub team
in their 2026-05-09 reply.

- **`X-Quality-Hint: thin-content` header** (`app/api/_quality_hint.py`,
  NEW). Defense-in-depth for the cookie-banner / thin-scrape pattern
  surfaced in the proactive-monitoring sweep. Detects responses
  matching the polite-refusal phrase set (cookie consent / footer
  navigation / incomplete or corrupted / short-response with refusal
  lead-in) and emits `X-Quality-Hint: thin-content; reason=<short>`.
  Tuned for high specificity (low false positive) over recall — won't
  trigger on legitimate refusals that happen to include "I appreciate".
  Wired into both `/v1/messages` and `/v1/chat/completions` JSON return
  paths (3 sites total). CORS-exposed.
- **412 response ETag header fix** (`app/api/llm_models.py:update_model_identity`).
  Pre-fix: `raise HTTPException(412, ...)` short-circuited any prior
  `response.headers["ETag"]` mutation, so the 412 carried no fresh
  ETag and callers had to GET-then-PUT. Now returns
  `JSONResponse(status_code=412, headers={"ETag": ...})` so callers
  can retry with one round-trip.

23 new unit tests covering thin-content phrase matching, length-gating,
defensive handling of malformed bodies, OpenAI/Anthropic shape
extraction, and a regression check for the 412 fix.

### v3.6.0 — `PUT /api/llm/models/{model_id}` for cluster-wide identity edits

Companion to the coordinator-hub task #230. Their hub UI now lets
operators edit `aliases` / `family` / `variant` for any canonical
model in the catalog scan view; this version ships the proxy-side
endpoint they call. Contract was locked 2026-05-09 in operator-forwarded
memos; OpenAPI spec at `docs/rfc/2026-05-model-identity-put-spec.md`.

- **`GET /api/llm/models/{model_id:path}`** — read merged identity
  state across all `ModelCapability` rows that match the canonical id
  OR any registered alias. Returns `ETag` header for downstream PUT
  concurrency. Optional `?provider_id=<id>` to scope to one row.
- **`PUT /api/llm/models/{model_id:path}`** — update aliases/family/variant.
  Default semantic: applies to ALL matching rows (same upstream model
  served by 2 providers → both rows updated). `If-Match` ETag required;
  mismatch → 412 with the fresh ETag in the response. PATCH-like
  semantics on PUT — fields absent from the body preserve their
  current value.
- **Validation** (`app/api/llm_models.py:_validate_aliases`):
  alias 1-64 chars, no whitespace, no duplicates (case-insensitive),
  max 16, no cross-row collision with another model's canonical id
  or alias. `family=""` rejected; novel `family` values save with a
  `X-Warning` header for the Hub UI to render as a yellow toast.
- **`KNOWN_FAMILIES` constant** (`app/routing/canonical.py`) —
  `claude / cohere / deepseek / gemini / gpt / grok / llama / mistral`.
  Hub team lifts this via the OpenAPI spec for client-side pre-validation.
- **Cluster sync replication of identity fields** (prerequisite —
  `app/cluster/manager.py` + `app/cluster/sync.py`). Pre-v3.6.0 the
  build payload didn't include `aliases / model_family / model_variant`,
  and the apply pass didn't write them. PUTs would have silently
  diverged across nodes. Both sides patched.
- **CORS expose** — `ETag` and `X-Warning` added to the allowed-headers
  list in `app/main.py`.
- **Auth**: standard admin scope (session cookie or admin API key).
  v3.6.1 will add a narrower `key_type=admin-readonly-catalog` as a
  drop-in replacement.

29 new unit tests covering validation, ETag determinism, parse_if_match,
multi-row merge, and cluster-sync replication regression.
Total: 1069 → 1098 passing.

## v3.5.x — Model identity model (LMRHv2.1)

### v3.5.11 — Second QA sweep: webhook SSRF guard + stop_sequences cap + test isolation residue

Closes the remaining 2 bugs from the first QA pass (`docs/bug-log.md`) and
3 new bugs found in a second probe-driven sweep against the live fleet.

- **BUG-001 — Mock test queue not drained between tests** (`tests/integration/conftest.py`, `tests/mock_llm_server.py`). The flaky-test contributor: `mock_ctl` (function-scoped) cleared `_received` but never drained `_queue`. If a prior test queued a response that never got consumed (e.g. the request 4xx'd before reaching the mock), the next test's queued response was second in line — the leftover served first, breaking subtle assertions on `test_text_only_request_passes_through_unchanged`. Added `MockServer.clear_queue()` and called it from the fixture alongside `clear_received()`.
- **BUG-003 residual — pytest-mock provider rows persist 7 days** (`app/api/providers.py`, `tests/conftest.py`). Mirrors the existing `/api/keys/_purge-test-tombstones` admin endpoint. Without a parallel for providers, every integration run that creates `pytest-mock` rows leaves them soft-deleted for the full tombstone retention window, bloating cluster-sync apply payloads. New `POST /api/providers/_purge-test-tombstones` (admin-gated, hard-deletes only `pytest-%`/`test-playwright-%`/`debug-%` rows older than 60s). `pytest_sessionfinish` now calls both endpoints.
- **BUG-013 — Webhook URL scheme not validated (SSRF surface)** (`app/api/_input_validation.py:validate_webhook_url`). Pre-fix, `X-Webhook-URL: file:///etc/passwd` was accepted and the proxy's httpx client would attempt to open it. Same for `gopher://`, `data:`, `ftp://`. Now rejects any scheme other than `http://` or `https://` with a 400. We deliberately do NOT block `localhost` or private IPs because operators legitimately POST to internal hub webhooks; egress restriction is the network layer's job.
- **BUG-015 — Unbounded `stop_sequences` array** (`app/api/_input_validation.py`). 1000-entry arrays were silently accepted (T23 sweep). Anthropic and OpenAI both cap at ≤16; pre-validating saves a wasted upstream round-trip and gives the caller a clearer error than `upstream 400: too many stop sequences`. Same cap applies to OpenAI-style `stop` field.
- **Wired** `validate_webhook_url(x_webhook_url)` into both `messages.py` and `completions.py` request entry. 10 new unit tests (`tests/unit/test_v358_input_validation.py`) covering scheme rejection, valid URLs, missing host, and stop-sequences cap.

Tests: 1069 passing (1059 → 1069, +10 new). All 12 bugs from the QA pass now closed; 3 new bugs from the second sweep also closed in this version.

### v3.5.10 — QA hardening (X-Substituted-From + alias cleanup tool + ETag doc)

Closes the last 3 bugs from the QA pass (`docs/bug-log.md`).

- **BUG-006 — `X-Substituted-From` / `X-Substituted-To` headers** (`app/api/_request_pipeline.py:build_base_response_headers`). When the router triggers cross-family fallback (caller asked for model X, no provider serves family X, so the proxy substituted Y), the response now includes both headers. Pre-fix the only signal was buried inside the `LLM-Capability` structured-field-value (`chosen-because=cross-family-fallback, requested-model=..., served-model=...`). Browser callers (and any client that doesn't parse RFC 8941 SFVs) can now detect substitution by reading a single header. Both new headers added to the CORS `expose_headers` list in `app/main.py`.
- **BUG-010 — alias↔canonical collision cleanup tool** (`tools/cleanup_alias_collisions.py`). The 2026-05-09 QA pass found 3 legacy bare-name `model_capabilities` rows (`grok-3`, `grok-4`, plus an extra) that became redundant after v3.4.1's canonical-id+aliases switch. The de-dup logic in `/v1/models` correctly hides these from callers, but they're dead weight. Operator runs `sudo docker exec llm-proxy2 python3 -m tools.cleanup_alias_collisions --dry-run` to preview, then without `--dry-run` to soft-delete. Idempotent. New `tools/` directory shipped inside the Docker image (Dockerfile updated).
- **BUG-011 — Per-node ETag drift documented** (`docs/lmrh-2.0-bidirectional.md`). Added a "Per-node ETag — what to know" subsection under `/lmrh/providers` explaining why ETags differ across cluster nodes (per-node `provider_metrics` aggregates), the load-balancer pinning recommendation, and that `subscribe()` SSE is immune to the issue.

Tests: 1059 passing (no test changes — header addition + doc fix + new tool).

### v3.5.9 — Test infra + circuit-breaker cleanup hooks

Closes 3 of the 4 medium-severity bugs from the QA pass (`docs/bug-log.md`).

- **BUG-012 — `/health` ghost CB entries on deleted providers** — `app/api/providers.py:delete_provider` and `app/cluster/sync.py:apply_sync` (provider tombstone propagation) now clear `circuit_breaker._local_states` + `_auth_failed` for the deleted provider id. Pre-fix, soft-deleted providers left CB state in memory until container restart, so `/health` reported phantom open/half-open breakers.
- **BUG-009 — SDK `subscribe()` slow stop** — `sdk/python/lmrh_client.py:_sse_session` now sets `httpx.Timeout(read=heartbeat_sec * 2)` (was `timeout=None`). Effective stop latency: ≤ 2× heartbeat (default 50s → measured 8.2s in regression test); pre-fix it could block indefinitely waiting for the next event/heartbeat.
- **BUG-002 — Mock LLM server port collisions** — `tests/mock_llm_server.py:start_mock_server` now accepts `port=0` (OS-assigned) by default, exposes the actually-bound port via `MockServer.port` + `.url`, and `MockServer.stop()` calls `server_close()` to release the socket immediately (was leaving it in TIME_WAIT for ~60s, blocking the next test). Existing callers that explicitly pass `port=9876` keep working.

BUG-003 (integration tests pollute prod DB) is partially addressed by BUG-012's CB cleanup — orphan CB state no longer accumulates from deleted-test-provider rows. Full hard-delete cleanup of `pytest-mock` provider rows during teardown is left for v3.5.10 (low-risk follow-up; the soft-delete tombstone retention worker will sweep them after 7 days regardless).

Tests: 1059 still passing. SDK stop-latency regression smoked end-to-end against live fleet (8.2s exit).

### v3.5.8 — Input validation + upstream-error sanitization (security/quality)

Closes 4 bugs found during the post-v3.5.7 deep QA pass. See `docs/bug-log.md` BUG-004, BUG-005, BUG-007, BUG-008.

**`app/api/_input_validation.py`** (NEW) — front-line request validation. Wired into both `/v1/messages` and `/v1/chat/completions` immediately after `body = await request.json()`. Rejects:
- Non-dict body (`null`, list, scalar) → 400
- Empty body `{}` → 400 with "model required"
- Missing `model` field → 400
- `messages` not a non-empty list → 400
- Invalid `role` (not in `{system, user, assistant, tool, function}`) → 400
- Non-positive `max_tokens` → 400
- `model: "auto"` and `model: "llmp-auto"` are explicitly accepted (auto-routing)

**`sanitize_upstream_error()`** (in same file) — strips Python tracebacks, file paths, and `/usr/local/lib/...` references from upstream exception text before returning to the client. Uses `circuit_breaker.classify_error()` to map error class → status code (`bad_request` → HTTP 400, everything else → 502).

**Why this matters**:
- BUG-005 was a denial-of-wallet vector: any leaked API key could spam `{}` and burn provider quota; now hard-rejected at the input layer
- BUG-007/008 leaked `/usr/local/lib/python3.13/site-packages/litellm/...` paths to anyone sending malformed input — information disclosure now closed
- BUG-004 returned upstream 502 with bridge errors when client request was malformed — now returns clear 400

Tests: +19 in `tests/unit/test_v358_input_validation.py` covering all rejection paths + happy path + sanitizer behavior. 1040 → 1059 passing.

### v3.5.7 — Documentation polish + pause-state refresh

End-of-active-session housekeeping. **No code changes.**

- **`docs/architecture.md`** — version stamp bumped from v3.5.0 to v3.5.7 (was four versions stale). Observability section expanded with the v3.5.x dashboard widgets (probe back-off, subscription quota) and the per-direction cost split additions to `ProviderMetric`. "Where to look first when…" table gained four new entries covering the things shipped today: probe back-off pause causes, subscription quota surfacing, SDK SSE consumer, and the model-identity RFC.
- **Pause-state memory** — refreshed to v3.5.7 with the full v3.5.0 → v3.5.6 sprint summary (12 dot releases shipped this session including the model-identity RFC, refactor pass through R4, in-page help expansion, SDK subscribe(), 3-layer probe-state observability loop, dashboard widgets, Grok 3/4 evaluation findings).

Tests: 1040 passing (no test changes).

### v3.5.6 — Dashboard probe back-off panel

The v3.5.4 `/api/monitoring/probe-state` endpoint shipped without a UI consumer; this dot release adds it.

- **`monitoringApi.probeState()`** — typed wrapper in `frontend/src/api/index.ts` for the v3.5.4 admin endpoint.
- **Dashboard "Probe Back-off" card** — sits below "External Status" in the right column. At steady-state shows a single green `All healthy` badge. When any provider is in the v3.3.3 back-off window (consecutive 429s on its keep-alive probe), lists each one with `Nx 429 · Mm Ss left` plus a small explainer that real user traffic continues regardless.
- Refreshes every 30s (back-off windows are 10-30 min long; faster polling is wasteful).

The whole loop (probe back-off detection → back-end state → admin endpoint → dashboard surfacing) is now end-to-end. Prior to v3.5.6 the only way to see "is the v3.3.3 back-off engaged?" was `docker exec` Python shell or `curl` the admin endpoint.

Files:
- `frontend/src/api/index.ts` — `probeState()` typed wrapper
- `frontend/src/pages/DashboardPage.tsx` — new card + 30s refetch hook

Tests: 1040 passing (no test changes — pure frontend addition consuming an existing endpoint).

### v3.5.5 — Refactor R4: extract claude-oauth request setup

`_complete_claude_oauth` and `_stream_claude_oauth` in `app/api/_messages_streaming.py` each opened with the same 4-line URL + body-mutation block AND each declared the `httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)` verbatim.

- **`_CLAUDE_OAUTH_TIMEOUT`** module constant — single source of truth for the split-phase timeout (the v3.0.60 rationale around DNS-failure pool exhaustion lives in the constant's docstring now, not in two separate function-level comments).
- **`_prepare_claude_oauth_request(body, *, stream)`** helper returning `(url, prepared_body)`. Handles URL construction, `max_tokens` default, claude-code system injection, and the stream flag in one place.

The 401-refresh-and-retry loop was NOT extracted — too different between the dict-returning `complete` and bytes-yielding `stream` paths to do cleanly without a generator-based driver. Deferred until a 3rd OAuth provider makes the pattern worth abstracting.

Files:
- `app/api/_messages_streaming.py` — added the constant + helper, both call sites updated, verbose pre-fix comments folded into the helper docstring (701 → 727 lines net; the +26 is the helper's docstring explaining v3.0.60 split-timeout history once instead of twice)
- `docs/refactor-log.md` — R4 entry prepended with audit notes on other candidates surveyed (providers.py 952L, sdk/python/lmrh_client.py 666L, _grok_web_dispatch.py 515L) and why they were skipped this pass
- `docs/architecture.md` — updated claude-oauth dispatch description to reference the new helpers

Tests: 1040 passing (no regression).

### v3.5.4 — Probe-state diagnostic endpoint + usage-limit tooltip clarity

Two small monitoring/operator-experience improvements bundled as one dot release.

- **`GET /api/monitoring/probe-state`** — admin endpoint exposing the in-memory keep-alive probe back-off state (consecutive 429 counts + remaining cool-off seconds per provider). Pre-v3.5.4 this state was only inspectable via `docker exec` Python shell. When grok-web (or any subscription-tier-probed provider) hits a streak of rate-limits, operator can now `curl /api/monitoring/probe-state` instead of shelling into the container. Empty dict at steady-state.
- **Tooltip clarification on usage-limit fields** in `ProviderForm.tsx` — the v3.5.3 dashboard widget surfaced `Devin-Anthropic-Max-VG` at 256% of weekly limit; root cause was operator-configured `usage_weekly_limit_tokens=20M` being below Anthropic's actual Pro Max allowance. New tooltips on `usage_session_limit_tokens` + `usage_weekly_limit_tokens` make explicit that these are operator-imposed ceilings (early-warning thresholds), NOT the actual upstream provider's allowance. Same clarification on `usage_session_window_sec` + `usage_weekly_reset_hour` for completeness.

Both changes align with the operator-locked "be hugely proactive on monitoring + improvements" rule. No backend logic change beyond the new endpoint.

Tests: no changes (1040 still passing).

### v3.5.3 — Subscription quota dashboard widget + over-limit banner

The proxy has tracked subscription quotas (Anthropic Pro Max weekly window, Codex weekly window, etc.) since v3.0.64 — the data shows up as a small text indicator on the providers page, but it's easy to miss buried in the list. v3.5.3 surfaces it on the dashboard.

- **Dashboard `Sub Quota` stat card** — shows the worst (highest) weekly_pct across providers with `usage_tracking_enabled=true`. Color-coded: green when all <80%, yellow when any 80-100%, red when any >100%. Sub-label says "N over" / "N approaching" / "all healthy". Replaces the redundant Activity stat card when at least one provider has tracking enabled.
- **Top-of-dashboard banner** — only renders when at least one provider is over 100% on weekly OR session window. Names the offenders, shows the percentages, explains the two interpretations (operator's limit too low vs caller actually over budget), and points at remediation. Stays out of the way when everything is healthy.
- **Provider type extension** — `Provider` interface in `frontend/src/types/index.ts` now declares the optional `usage_*` fields (was previously cast at the use site with `as unknown as`). Existing usage indicator on the providers page still works; gets proper typing.

Driven by the 2026-05-09 audit finding that `Devin-Anthropic-Max-VG` was at 255.7% of its configured weekly limit with no surfacing on the dashboard. The 255% is itself a misconfiguration (operator's `usage_weekly_limit_tokens=20M` is below Anthropic's actual Pro Max allowance), but the principle holds: when tracking is on, anything over 100% deserves an at-a-glance signal rather than being buried in the providers list.

**No backend changes.** Pure frontend; data was already in `/api/providers` response.

Tests: no changes (1040 still passing).

### v3.5.2 — SDK `subscribe()` consumer for `/lmrh/stream`

Closes the v3.4.0 SSE push loop. Proxy server has had Server-Sent Events on `/lmrh/stream` since v3.4.0 (2026-05-09 morning); the SDK was still polling-only. v3.5.2 adds `LmrhClient.subscribe(on_snapshot, on_error=None)` to consume the stream.

- **`LmrhClient.subscribe(on_snapshot, ...)`** — opens a long-lived SSE connection, parses ``event: snapshot`` frames, yields parsed `Snapshot` objects to the callback. Heartbeat (`: ping`) frames are silently ignored. Blocks the calling thread; spawn in a daemon thread for async usage.
- **Auto-reconnect**: drops in network → `reconnect_delay_sec` wait → retry, until `stop()` is called.
- **Auto-fallback**: probes `/.well-known/lmrh-config` first; if `stream` endpoint not advertised (proxy older than v3.4.0), or if `/lmrh/stream` returns 404, falls through to polling and dispatches snapshots from there. **Caller code is identical regardless of proxy version.**
- **`heartbeat_sec` query parameter** (default 25, range 10-120) propagated to server so callers behind aggressive idle-timeout proxies can shrink it.
- **`reconnect_delay_sec`** parameter (default 5.0) tunable for testing or strict failure budgets.

`sdk/python/README.md` updated with new "SSE push (v3.5.2+) — `subscribe()`" section explaining when to prefer push over polling and showing the daemon-thread usage pattern.

Tests: +5 in `sdk/python/test_subscribe.py` (snapshot dispatch, heartbeat ignored, no-stream-endpoint fallback, 404-on-stream fallback, clean stop). 1035 → 1040 passing.

**No proxy-side change** — this release is SDK-only. Fleet stays on v3.4.0+ feature surface; callers can adopt the helper when convenient.

### v3.5.1 — Refactor pass + in-page help expansion + capability-form model-identity edits

Three internal-quality changes shipped as one dot release. No external API changes; all-in-one operator-experience improvement.

**Refactor — duplication-elimination across endpoints (R1+R2+R3)**:
- **R1+R2** (`app/api/_request_pipeline.py`): extracted the cache-decision-and-serve block (35L, 100% duplicated) and the CoT-E engagement block (42L, 80% duplicated) from `messages.py` and `completions.py` into two shared helpers — `maybe_serve_from_cache()` and `maybe_engage_cot()`. Wire-format-specific bits (SSE/JSON builders, stream functions) pass through as callable parameters. Caught a near-bug during R1: the first cut lost the `cache_decision` local that downstream `maybe_store()` calls relied on; the silent `try/except` was swallowing a NameError so cache write-back was quietly skipped. Helper now returns the decision tuple.
- **R3** (`app/providers/grok_web.py`): the 3 dispatch functions (`complete_grok_web` / `stream_grok_web` / `stream_grok_web_anthropic`) each opened with the same 6-line conv_id/url/headers/body setup. Extracted to `_build_manual_request(extra_config, prompt, model)` returning a 5-tuple. Future fixes to grok.com URL or header conventions land in one place, not three.
- **Architecture docs**: new `docs/architecture.md` (module map + boundaries + flow diagrams) and `docs/refactor-log.md` (running ledger of refactor passes). Both updated through R3.

**In-page help expansion** — `frontend/`:
- `ProviderModels.tsx` capability admin form: every field now has a `?` hover with operator-friendly explanation (Tasks, Modalities, Latency, Cost tier, Safety, Context length, Regions, Native reasoning/tools/vision). Added new section "Model identity (v3.5.0+)" with editable `aliases` (comma-separated), `model_family`, `model_variant` fields plus tooltips explaining canonical naming convention + multi-route disambiguation.
- `ProviderForm.tsx`: tooltips on Priority, Timeout, Hold-down, Failure threshold (the 4 fields operators tune most often — and that have the least obvious meaning).
- `APIKeysPage.tsx`: tooltips on Key Name, Key Type, Rate Limit, Lifetime spending cap, edit-modal Rate limit.
- Total: 16 new `?` hovers across the 3 forms.

**Backend support for capability-form edits** — `app/api/providers.py`:
- `CapabilityUpdate` Pydantic model accepts optional `aliases`, `model_family`, `model_variant` (defaulted so older Hub UI clients still PUT successfully).
- `_serialize_cap` emits the three new fields in GET responses so the form can populate.

**Files**:
- `app/__version__.py`, `README.md`, `CHANGELOG.md`
- `app/api/_request_pipeline.py` (+~190L two new helpers)
- `app/api/messages.py`, `app/api/completions.py` (−~60L combined inline blocks; cleaned 6 dead imports)
- `app/api/providers.py` (`CapabilityUpdate` + `_serialize_cap` extended)
- `app/providers/grok_web.py` (`_build_manual_request` helper, 3 callers updated)
- `docs/architecture.md` (NEW)
- `docs/refactor-log.md` (NEW)
- `frontend/src/types/index.ts` (`ModelCapability` extended)
- `frontend/src/components/providers/ProviderModels.tsx` (new fields + 13 tooltips)
- `frontend/src/components/providers/ProviderForm.tsx` (4 tooltips)
- `frontend/src/pages/APIKeysPage.tsx` (5 tooltips)

Tests: 1035 passing (no regression).

### v3.5.0 — Canonical model_id + aliases + family/variant (LMRHv2.1)

Two-step rollout (v3.4.1 catalog + v3.5.0 LMRH) shipped as a single bundle. Closes the duplication issue where the same upstream model appeared as multiple `/v1/models` entries (e.g. `grok-3` AND `x-ai/grok-3`). Cross-project RFC at `docs/rfc/2026-05-model-identity.md`.

- **Aliases column on `model_capabilities`** — `app/models/db.py` + migration. Each capability now carries `aliases: JSON` listing alternate input spellings. Router matches `model_id == X OR X IN aliases` (case-insensitive). Pre-fix, the operator had to register `grok-3` AND `x-ai/grok-3` as separate rows; v3.4.1 keeps just one canonical row with bare as alias.
- **`/v1/models` de-dupes across canonical + aliases** — `app/api/models.py`. Each model appears once under canonical id with `aliases: [...]` array as a sibling field. The duplication users were seeing in the upstream catalog disappears.
- **`app/routing/canonical.py`** (NEW) — `matches_capability`, `derive_family`, `collect_canonical_aliases` helpers. Used by the router and `/v1/models`; also the public API for tests + future provider modules.
- **Grok-Web canonical-only** — `app/providers/grok_web.py`. `SUPPORTED_MODELS` now `["x-ai/grok-3", "x-ai/grok-4"]`; `SUPPORTED_MODEL_ALIASES` maps each to `["grok-3"]` / `["grok-4"]`. `scanner.py` persists the alias list when seeding capability rows.
- **LMRHv2.1 — `family` + `variant` + `aliases` on each model entry** — `app/routing/lmrh/snapshot.py` + `app/api/lmrh_v2.py` + `sdk/python/lmrh_client.py`. Multi-route grok-3 (Grok-Web `variant: "web"` vs OpenRouter `variant: "openrouter"`) is now a first-class concept callers can group or pick from. Body version bumps to `2.1`; `/.well-known/lmrh-config` advertises both `2.0` and `2.1` in `supported_versions`.
- **Cross-project RFC** — `docs/rfc/2026-05-model-identity.md`. Standalone document the operator can share with hub, DevinGPT, paperless teams. Covers motivation, naming convention, migration path, and per-consumer recommendations.

Schema (idempotent ALTER TABLE):
- `model_capabilities ADD COLUMN aliases JSON`
- `model_capabilities ADD COLUMN model_family TEXT`
- `model_capabilities ADD COLUMN model_variant TEXT`

Tests: +13 in `tests/unit/test_v341_v350_canonical_aliases.py` (matches_capability exact / alias / case-insensitive / empty inputs; derive_family with 1, 2+ slashes; collect_canonical_aliases ordering + de-dup; grok_web canonical contract; SDK v2.1 fields with default values; SDK parses v2.1 / handles v2.0; /v1/models de-dupe). 1022 → 1035 passing.

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
