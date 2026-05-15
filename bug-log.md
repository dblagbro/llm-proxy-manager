# Bug Log — llm-proxy-v2

Persistent log of defects, regressions, and quality gaps discovered during
QA/regression sweeps. Add new findings at the top with the most recent
sweep date as the section header.

Severity ladder: **critical** > **high** > **medium** > **low** > **enhancement**

Status flow: **open** → **in-progress** → **fixed** → **verified-fixed** → **wont-fix**

---

## 2026-05-15 — post-refactor deep regression sweep (v3.10.9)

Deep regression / release-hardening sweep covering **v3.9.16 → v3.10.9**
(14 releases shipped since the last QA pass, including the `messages.py`
→ `_messages_dispatch.py` extraction). Environment: 3-node prod cluster
all on v3.10.9, healthy. **1969/1969 unit tests pass**; integration
(non-UI) **64 passed / 2 failed / 16 skipped**. Findings BUG-023+
(BUG-001..022 already used). Methods: full pytest suites, adversarial
HTTP probing of every endpoint, code-level regression audit of the
14-release diff, live container-log inspection on all 3 nodes.

> **Remediation pass (v3.10.10, 2026-05-15 PM)** — BUG-025, BUG-030,
> BUG-034 fixed; BUG-023 re-investigated and the auth-bypass claim
> **retracted** (see below); BUG-037 added for the real defect that the
> `test_revoke_key_rejects_llm_calls` failure pointed at. 1973/1973 unit
> tests pass.

### BUG-023 [retracted → hardening] Revoked-key auth — "still authenticates" claim DISPROVEN

- **Area**: `app/auth/keys.py::verify_api_key`
- **Original claim (2026-05-15 sweep)**: a revoked key still authenticates (HTTP 200) for a window after deletion; attributed to a missing `deleted_at` filter + cluster-sync resurrection.
- **Re-investigation (v3.10.10)**: **disproven.** Direct probe — create key → soft-delete it into the exact state `delete_key` produces (`enabled=False`, `deleted_at` set) → immediately re-use the key: `/v1/models` → **401 in 0.0s**, `/v1/messages` → **401 in 0.0s**. `verify_api_key` filters `enabled == True`, `delete_key` sets `enabled=False`; the soft-delete revocation path is correct and fast. The `test_revoke_key_rejects_llm_calls` failure that triggered this entry was a **read timeout**, not an auth bypass — its root cause is BUG-037 (an unregistered model id hangs the dispatch). The earlier "HTTP 200" claim was an unverified inference, not an observation.
- **What was genuinely real**: `verify_api_key` did not filter `deleted_at IS NULL` — a gap *only* if a tombstoned row is somehow left `enabled=True` (e.g. a cluster-sync merge resurrecting it). Worth closing as defence-in-depth.
- **Fix shipped (v3.10.10)**: `verify_api_key` query now also filters `ApiKey.deleted_at.is_(None)`, so a tombstoned row can never authenticate regardless of its `enabled` flag. Unit tests in `tests/unit/test_v31010_buglog_fixes.py` (healthy accept / disabled reject / soft-deleted-but-enabled reject).
- **Still open**: cluster-sync ApiKey merge — confirm `delete_key` bumps the LWW timestamp the merge keys on; add a two-node delete→sync convergence test (GAP-4).
- **Status**: fixed (hardening) in v3.10.10 — auth-bypass claim retracted; cluster-sync convergence test still owed.

### BUG-024 [HIGH] Stale `extra` after OAuth→litellm fallthrough — wrong credentials on the litellm call

- **Area**: `app/api/messages.py` (`extra` build vs litellm dispatch), `_messages_dispatch.py`
- **Repro**: every claude-oauth provider 401/403s so `dispatch_claude_oauth_chain` exhausts the OAuth chain and returns `(None, route)` with `route` advanced to a litellm provider
- **Expected**: the litellm dispatch uses the new provider's `litellm_kwargs` (api_key, base_url, headers)
- **Actual**: `extra` is built once (`messages.py` ~line 294) from the *original* claude-oauth route's `litellm_kwargs`, before the dispatch call (~line 362). After fallthrough `route` is new but `extra` is stale → the litellm call uses `route.litellm_model` (correct) with the old route's credentials/headers (wrong).
- **Evidence**: code audit of the v3.9.16 baseline confirms the same ordering — **pre-existing defect, NOT a v3.10.9 regression**. The v3.10.9 docstring ("caller falls through to the litellm path with that route") reads as if the fallthrough is sound, masking it.
- **Likely cause**: `extra`/`system`/`tools` computed before the dispatch branch and never recomputed when `route` changes.
- **Recommended fix**: after `dispatch_claude_oauth_chain` returns, if `route` changed, rebuild `extra` (and `system`/`tools`) from the new route. Needs runtime confirmation — rare path (requires all OAuth providers to fail auth).
- **Status**: open (likely bug — confirm at runtime)

### BUG-025 [MEDIUM] Malformed / empty JSON body → bare HTTP 500 on `/v1/messages` + `/v1/chat/completions`

- **Area**: `app/api/messages.py`, `app/api/completions.py`
- **Repro**: `POST /v1/messages` with body `{bad` or empty `''`
- **Expected**: 400 with a `{"detail": ...}` JSON error
- **Actual**: uncaught `json.decoder.JSONDecodeError` → **HTTP 500**, plain-text body. (`/api/auth/login`, which uses a Pydantic body model, handles the same input correctly with 422.)
- **Evidence**: container log traceback — `messages.py body = await request.json() … JSONDecodeError`.
- **Likely cause**: `body = await request.json()` is unguarded; the v3.5.8 input validator runs *after* it, so it never sees malformed input.
- **Recommended fix**: wrap `request.json()` (or add a global `@app.exception_handler` for `JSONDecodeError`) → 400. Closes it for every raw-body endpoint at once.
- **Fix shipped (v3.10.10)**: global `@app.exception_handler(json.JSONDecodeError)` in `app/main.py` → 400 with a `{"error": {...}}` JSON envelope; closes it for every raw-body endpoint at once. Unit-tested in `test_v31010_buglog_fixes.py`.
- **Status**: fixed in v3.10.10

### BUG-026 [MEDIUM] AI-supervisor recursion guard is inert — supervisor pollutes its own stats

- **Area**: `app/monitoring/ai_provider_supervisor.py`
- **Repro**: supervisor's `classify_with_llm` self-calls `/v1/messages` with header `X-Internal-Source: ai_provider_supervisor`
- **Expected**: those internal classifier calls are excluded from provider stats / activity-log aggregates (the module docstring claims they are "filterable")
- **Actual**: **no code anywhere reads `X-Internal-Source`** — grep of `messages.py`, `_request_pipeline.py`, `ai_provider_supervisor_stats.py` finds zero consumers. The classifier calls land in `activity_log` as ordinary `llm_request` rows and are counted by both `compute_provider_stats` and the v3.10.4 error-rate sampler.
- **Evidence**: code audit. Low volume today (suggest-only, www01, 30-min cadence) but a failing classifier model would self-pollute the very stats driving its verdicts — and inflate the error-rate alert.
- **Recommended fix**: filter `event_meta`/header `X-Internal-Source` out of `compute_provider_stats` and `_sample_error_rate`'s queries; OR tag those rows with a distinct `event_type`.
- **Status**: open

### BUG-027 [MEDIUM] Integration test `test_release_now_also_enables_v386` fails deterministically

- **Area**: `tests/integration/test_manual_override_flow.py` / manual-override "release all to AI control" flow (v3.8.6)
- **Repro**: `pytest tests/integration/test_manual_override_flow.py::test_release_now_also_enables_v386`
- **Actual**: deterministic failure (re-ran twice). Not yet root-caused — could be a v3.8.6 behaviour regression or environmental (depends on current provider override state on the cluster).
- **Recommended fix**: triage — capture actual vs expected; determine regression vs environment.
- **Status**: open (needs triage)

### BUG-028 [MEDIUM] Cross-family translator still mishandles two message shapes

- **Area**: `app/api/_oauth_chat_translate.py`
- **Detail**: beyond the v3.10.0 fix — (a) an Anthropic assistant block with no text and no tool_use translates to `{"role":"assistant","content":null}` with no `tool_calls`, which OpenAI rejects; (b) `tool_result` → `role:"tool"` is emitted without verifying it *immediately follows* the matching assistant `tool_calls` — a misordered (not orphaned) pair still produces an OpenAI 400. The `known_tool_use_ids` pre-scan only catches fully-orphaned ids.
- **Evidence**: code audit.
- **Recommended fix**: emit a placeholder for empty assistant blocks; validate tool-message adjacency (or reorder) in `anthropic_messages_to_openai`. Add regression tests with both shapes.
- **Status**: open

### BUG-029 [MEDIUM] `/lmrh/quotes?model=<unknown>` returns 200 with empty `model_id` instead of an unknown-model error

- **Area**: `app/api/lmrh_v2.py`
- **Repro**: `GET /lmrh/quotes?model=this-model-does-not-exist-xyz` (auth'd)
- **Expected**: 4xx / explicit "unknown model" so a caller pre-flighting a typo gets a true signal
- **Actual**: 200 with `candidates:[{"model_id":"","score":888.0,...}]` — silently falls back to auto-routing the default provider.
- **Recommended fix**: when no capability matches the requested model, return 404/422 with a clear message.
- **Status**: open

### BUG-030 [LOW] `GET` on POST-only LLM endpoints returns 200 + SPA HTML instead of 405

- **Area**: `app/main.py` SPA catch-all
- **Repro**: `GET /v1/messages` or `GET /v1/chat/completions`
- **Actual**: 200 with the React `index.html`. (`PUT`/`DELETE` correctly 405 — only `GET` is swallowed by `@app.get("/{full_path:path}")`.)
- **Recommended fix**: exclude `/v1/*` (and `/api/*`) prefixes from the SPA catch-all, or register explicit 405 handlers.
- **Fix shipped (v3.10.10)**: `spa_catch_all` now returns a JSON 404 for any path under the `v1/`, `api/`, `cluster/`, `lmrh/`, `metrics`, `health`, `version` namespaces instead of the SPA HTML shell — non-browser API clients no longer parse a 200 HTML page as a success body. (A true 405 for the wrong-method-on-an-existing-route case is not attempted; a JSON 404 is the correct-enough fix.)
- **Status**: fixed in v3.10.10

### BUG-031 [LOW] `GET /api/providers/_refresh-all-anthropic-billing` returns 404 "Provider not found" instead of 405

- **Area**: `app/api/anthropic_billing.py` / `app/api/providers.py` route ordering
- **Detail**: the literal action path `_refresh-all-anthropic-billing` (POST-only) collides with `GET /api/providers/{provider_id}`, so a wrong-method GET is treated as a provider-id lookup → 404 "Provider not found".
- **Recommended fix**: move literal `_`-prefixed action endpoints off the `{provider_id}` namespace (e.g. `/api/providers/_actions/refresh-all-anthropic-billing`) or register the GET 405 explicitly. Low impact; a path-design smell.
- **Note (v3.10.10)**: the BUG-030 SPA-catch-all fix does **not** cover this — the path is matched by the real `GET /api/providers/{provider_id}` route, not the catch-all. The route-redesign above is the only real fix; deferred — it would change the endpoint URL and break the v3.9.19 "Refresh Usage Stats" button, so it is not a quick win.
- **Status**: open

### BUG-032 [LOW / hardening] ASGI + pool errors bypass `activity_log` — invisible to the v3.10.4 alert

- **Area**: observability — Starlette middleware errors, `sqlalchemy.pool` errors
- **Detail**: client-disconnect `Exception in ASGI application` (CancelledError / "Connection closed") and `sqlalchemy.pool` GC errors log at full-traceback stdlib `ERROR:` level. They (a) are indistinguishable from genuine ASGI faults when scanning logs, and (b) never reach `activity_log`, so the v3.10.4 error-rate alert is **blind** to them — a pool-exhaustion incident would not alert until it caused downstream request-level `severity=error` failures.
- **Recommended fix**: route ASGI exceptions through a handler that classifies client-disconnect as `warning` and real faults as `error`; emit a metric/alert hook for `sqlalchemy.pool` errors.
- **Status**: open

### BUG-033 [LOW] Orphan `tool_result` with image content silently drops the image

- **Area**: `app/api/_oauth_chat_translate.py::_tool_result_content_to_str`
- **Detail**: an orphaned `tool_result` whose content is an image block is flattened to the literal `"[image]"` — the image payload is silently discarded with no caller-visible signal.
- **Recommended fix**: at minimum document it; ideally translate the image to an OpenAI `image_url` part.
- **Status**: open

### BUG-034 [LOW] Inconsistent auth-error wording + `/lmrh/quotes` status inconsistency

- **Detail**: no-key responses say `"Missing API key"` on `/v1/messages` but `"missing api key"` (lowercase) on `/v1/models` and `/lmrh/*` — two `verify_api_key`/`resolve_api_key_dep` paths with divergent copy. `/lmrh/quotes` with a missing `model` → 422; with empty `model=` → 400 — same logical failure, two shapes; the `if not model` branch is partly dead (FastAPI rejects a missing required query first).
- **Recommended fix**: unify the auth-error string; pick one status for missing/empty `model`.
- **Fix shipped (v3.10.10)**: `resolve_api_key_dep` now raises `"Missing API key"` (was lowercase `"missing api key"`) — matches `verify_api_key`, so `/v1/models` and `/lmrh/*` no-key responses are consistent with `/v1/messages`.
- **Status**: partially fixed in v3.10.10 — auth-error wording unified; the `/lmrh/quotes` missing-vs-empty `model` status-shape inconsistency is unchanged (still open).

### BUG-035 [enhancement] `/v1/embeddings` Pydantic `list[float]` vs base64-`str` serializer warnings

- **Detail**: every `/v1/embeddings` call logs `PydanticSerializationUnexpectedValue` — the `embedding` field is declared `list[float]` but receives a base64 `str`. Response is 200; this is per-request log noise from a response-model mismatch.
- **Recommended fix**: widen the response model to `list[float] | str` (or split by `encoding_format`).
- **Status**: open

### BUG-036 [enhancement / hardening] `_messages_dispatch.py` (v3.10.9 refactor) has no behavioral test coverage

- **Area**: `tests/unit/test_v3109_messages_dispatch_extract.py`
- **Detail**: the v3.10.9 extraction moved the proxy's deepest hot path (claude-oauth chain walk, 401-refresh fallback, streaming pre-flight, empty-stream→502, network-error→next-provider, fallback-exhaustion) into `_messages_dispatch.py` (256 lines). The test file has 4 tests — 3 are source-grep wiring checks, 1 is behavioral but exercises only the trivial "route is not claude-oauth → fall through" path. **Zero behavioral coverage** of any dispatch branch. A "behaviour-preserving move" with no behavioural assertions cannot prove behaviour was preserved.
- **Recommended fix**: add mocked-chain tests for: 401→refresh→retry, network-error→next-provider, empty-stream→502, fallback-exhaustion→HTTPException, streaming pre-flight failure.
- **Status**: open

### BUG-037 [HIGH] `/v1/messages` for an unregistered model id can hang ~40s+ (300s server-side ceiling)

- **Area**: model routing / substitution + `app/api/_messages_streaming.py` (`_CLAUDE_OAUTH_TIMEOUT`)
- **Discovered**: v3.10.10 re-investigation of the `test_revoke_key_rejects_llm_calls` failure (originally misfiled as BUG-023).
- **Repro**: `POST /v1/messages` with a **valid** key for `model: claude-3-5-sonnet-20241022` — a model with **no `ModelCapability` rows** on this cluster. Probe result: request hung and the client timed out at 40s (it had not completed). A second observation routed the same model to `OpenRouter → openai/gpt-4o` and succeeded — i.e. the substitution target is non-deterministic, and at least one target path hangs.
- **Expected**: an unroutable / unregistered model id should fast-fail with a 4xx, or route to a working provider within a normal completion time.
- **Likely cause**: with no capability rows the router substitutes a provider; `_CLAUDE_OAUTH_TIMEOUT` carries a **300s read timeout** (`httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)`). If the substitute is a claude-oauth provider and the upstream hangs on the unrecognised model, the proxy can wait up to 5 minutes before failing.
- **Why it matters**: (1) a single hung request holds its connection — and DB session — for up to 300s; under load this is a plausible **contributor to ARCH-A** (pool exhaustion). (2) the integration suite's revocation test flapped on this, not on auth.
- **Recommended fix**: (a) fast-fail (400/404 "model not available") when a model id resolves to no capability and no deterministic route; (b) tighten the claude-oauth `read` timeout from 300s to a sane ceiling (e.g. 120s) — needs deliberate review as it touches the streaming hot path. NOT bundled into v3.10.10 (out of the "quick wins" scope).
- **Status**: open

### ARCH-A — UPDATE: leak is actively manifesting

The latent DB connection-pool leak (open since the v3.9.15 sweep) is
**live**. www01 container logs show, within a 3-hour window, **7×**
`ERROR:sqlalchemy.pool … Exception terminating connection` /
`The garbage collector is trying to clean up non-checked-in connection`,
correlated with `ValueError: Connection closed` from `aiosqlite`. A
pooled connection is escaping its `async with` and dying mid-use.
- Tracer (`DB_POOL_TRACE=1`) is enabled on www01 + www02; **NOT on GCP**
  — a node the v3.9.15 sweep named as historically affected. Enable it
  there.
- The `at`-scheduled `/cluster/db-pool-trace` capture (job #3, 2026-05-16
  15:00 UTC) should catch the per-connection acquisition stacks.
- **Status**: open — diagnostic toolkit in place; root cause still unknown.

---

## 2026-05-15 — bug-log audit refresh (v3.9.15)

Re-checked every item from the 2026-04-24 sweep against current code
(v3.9.14 → v3.9.15). Most issues were addressed by intermediate versions
without bug-log status updates; this section reconciles.

### Status reconciliation

| Bug | New status | Fixed in / by |
|---|---|---|
| BUG-001 | **open / deferred** | streaming-error contract needs cross-team coordination — see notes |
| BUG-002 | **verified-fixed** | `is_auth_error()` in `app/routing/circuit_breaker.py`; v3.7.16 #239 added DB-backed `auto_skip_until=now+24h` for persistent-auth providers |
| BUG-003 | **verified-fixed** | `refresh_and_persist` wired into both `_complete_claude_oauth` + `_stream_claude_oauth` (7 occurrences in `_messages_streaming.py`); 401 → refresh → retry |
| BUG-004 | **verified-fixed** | hardcoded `== "2.0.0"` removed; tests assert regex / read from `__version__.py` |
| BUG-005 | **verified-fixed** | `only_mock_routing` in `tests/integration/conftest.py` now toggles `enabled=False` on non-mocks then restores (v2.7.8 BUG-005 note in code) |
| BUG-006 | **verified-fixed** | injected Claude-Code marker carries `cache_control: ephemeral` (v2.7.6 BUG-006 note + 4-marker cap guard in `_count_cache_control_markers`) |
| BUG-007 | **fixed (v3.9.15)** | renamed to `_internal_refresh_access_token`; old name is a deprecation-warning alias; only known caller (burn test) migrated |
| BUG-008 | **verified-fixed** | same as BUG-003 — `refresh_and_persist` is the wire-up |
| BUG-009 | **verified-fixed** | README reflects current default-cred behavior |
| BUG-010 | **verified-fixed (backend) + UI badge present** | `normalize_priority_ties` + `_bump_priority_conflicts` in `app/api/providers.py`; UI warning per "v2.7.8 BUG-010" comment in `ProvidersPage.tsx` |
| BUG-011 | **verified-clean** | (was already closed) |
| BUG-012 | **fixed (v3.9.15)** | `--skip-destructive` flag added to `scripts/test_claude_oauth_live.py`; weekly automated runs can now skip refresh_token rotation |
| BUG-013 | **verified-fixed** | `app/__version__.py` is the single source of truth; FastAPI app factory + `/health` + `/cluster/status` + OTel tags all consume it |
| BUG-014 | **verified-fixed** | `app/api/monitoring.py` activity-log query uses `.in_(sev_list)` after `split(",")` |
| BUG-015 | **verified-fixed** | `app/main.py` SPA catch-all returns `Cache-Control: no-cache, must-revalidate` (v2.7.6 BUG-015 note) |
| BUG-016 | **verified-fixed** | `tests/integration/test_playwright_ui.py` locator matches actual UI text `Test OK` / `Test failed` |
| BUG-017 | **verified-fixed** | indexes present in `app/models/database.py` (`ix_api_keys_key_hash`, activity_log, provider_metrics) |
| BUG-018 | **verified-fixed** | `try_ranked_non_streaming` wired into both `messages.py` + `completions.py`; gated on `settings.fallback_enabled` |
| BUG-019 | **verified-fixed** | `_TYPES_REQUIRING_API_KEY` preflight in `create_provider` + `update_provider` |

**Net result**: 16 of 18 items closed. 2 remain:
- BUG-001 (deferred — needs DevinGPT/hub design sign-off on the streaming-error contract before changing wire behavior)
- ARCH-A new (latent DB connection leak — audit shows every `AsyncSessionLocal()` is `async with`-wrapped, so the leak isn't naive session management; needs more diagnostic data from the next live recurrence)

### v3.9.15 fixes (this release)

**BUG-007 — `refresh_access_token` rename**

- Root cause: the safe wrapper (`refresh_and_persist`) and the destructive
  primitive (`refresh_access_token`) shared the same module namespace with
  the destructive one having the more discoverable name. Autocomplete or
  casual `from app.providers.claude_oauth_flow import refresh_*` would
  pick the wrong one.
- Fix: renamed canonical name to `_internal_refresh_access_token`.
  Kept the old name as a one-release back-compat alias that emits
  `DeprecationWarning`. Migrated the one in-tree caller
  (`scripts/test_claude_oauth_live.py`) to import the new name via
  `as refresh_access_token` rebind so the rest of the script is
  unchanged.
- Static-analysis test in `test_v3915_remaining_buglog.py` walks
  `app/**/*.py` for any `import refresh_access_token` — fails loud if
  a future change re-introduces the bad import.

**BUG-012 — burn test `--skip-destructive` flag**

- Root cause: `t_refresh_and_persist` rotates the live refresh token on
  every run. If the rotation chain breaks, the next run can't proceed
  until admin re-auths. Operators who want a weekly read-only verify
  can't run the suite without consuming the token.
- Fix: `argparse` wired in the `__main__` block; `--skip-destructive`
  marks the destructive test as `_record(name, True, "skipped")` so
  the weekly job records a clean pass rather than a false failure.
- Locked by tests: signature, flag parsing, the destructive-test set,
  and the skipped-as-pass behavior.

### ARCH-A — latent DB connection leak (NEW open item)

- **Subsystem**: background workers + cluster sync
- **Symptoms (observed today)**: www01 + GCP both saturated their
  `QueuePool` after 13h / 20h respectively post-deploy. /health
  started returning 500 from auth lookups blocked on a full pool.
- **Audit done in this sweep**: every `AsyncSessionLocal()` call site
  in `app/` is inside `async with`. So the leak isn't a naive
  unmanaged-session bug.
- **Hypotheses still in play**:
  1. A worker that opens `engine.connect()` directly (rare pattern)
  2. A long-lived task that holds a session across a hung `await`
     (e.g. a Redis or upstream-API call that doesn't time out)
  3. A streaming response that retains a session reference until SSE
     client disconnects — and the disconnect detection has a leak path
- **Mitigations already in place**:
  - v3.9.8: pool state exposed in `/health.dbPool` (size/checked_out/overflow)
  - v3.9.10: Prometheus gauges + 30s background sampler
  - v3.9.12: `tools/cut-release.sh` for fast diagnosis-restart cycles
- **Next investigation step**: when the next saturation event occurs,
  capture `engine.pool.checkedout()` mid-event + `select * from
  pg_stat_activity` equivalent (SQLite has `PRAGMA database_list` /
  per-connection state via `sqlite_master`) to identify which
  long-held queries are holding the connections. Filed for the next
  recurrence.

### BUG-001 — streaming error contract (DEFERRED)

- **Subsystem**: `_messages_streaming.py`, `_completions_streaming.py`
- **Root cause**: streaming dispatch returns HTTP 200 (the first SSE
  chunk has already left), then emits a terminal `data: {"type":"error"}`
  + `message_stop` when upstream fails. Clients that check
  `r.status_code` see success and an empty stream.
- **Why deferred**: changing the wire contract requires sign-off from
  DevinGPT (just adopted streaming write-back in v3.9.11) and hub
  (considering RMAI adoption). Two viable shapes:
  1. Pre-stream errors return non-200 BEFORE the first chunk
  2. Post-stream errors emit a custom `X-Stream-Error: true` header on
     the SSE response so clients can fail-loud
- **Plan**: file an RFC, get sign-off, ship in v3.10.x. Until then the
  inline-SSE-error behavior remains stable (DevinGPT depends on it).

---

## 2026-04-24 — post-v2.7.5 deep regression sweep

Driver: comprehensive post-OAuth-rollout validation. Production cluster
on v2.7.5 across 3 nodes. Devin-VG provider configured. 633 unit tests
passing; 7 integration tests failing on first run (analyzed below).

### BUG-001 [CRITICAL] Streaming requests mask auth/upstream errors with HTTP 200

- **Area**: `/v1/messages` streaming path, `app/api/_messages_streaming.py`
- **Repro**:
  1. Configure or have an enabled anthropic provider with a stale/invalid `x-api-key`
  2. POST `/v1/messages` with `stream: true` so it routes to that provider
- **Expected**: HTTP 5xx OR automatic failover to the next-priority anthropic-capable provider
- **Actual**: HTTP **200**, SSE body is exactly:
    ```
    data: {"type": "error", "error": {"message": "litellm.AuthenticationError ... invalid x-api-key ..."}}
    data: {"type":"message_stop"}
    data: [DONE]
    ```
- **Impact**: Clients that only check status_code see "success", consume an empty stream, and surface a confusing UX. Auth misconfiguration becomes invisible until users complain.
- **Likely cause**: streaming path catches exceptions from the upstream call but emits an SSE error event and a synthetic `message_stop` instead of (a) returning a non-200 status before the SSE starts, or (b) entering the failover ladder.
- **Suggested fix**:
    - For pre-stream auth errors (401/403), return an HTTP error status BEFORE the body starts streaming.
    - Inside the SSE stream, on a fatal upstream error, attempt failover to the next-priority capable provider. Only emit an SSE error event if all candidates fail.
    - Mark provider failures as failures in the circuit breaker (currently uncertain — see BUG-003).
- **Status**: open

### BUG-002 [HIGH] Persistent auth_error not auto-disabling broken providers

- **Area**: provider lifecycle / circuit breaker
- **Repro**: `POST /api/providers/{id}/test` against the two broken anthropic providers (`Anthropic Claude Code #3`, `C1 Anthropic Claude`) returns `success=false` with `litellm.AuthenticationError ... invalid x-api-key`. The providers remain `enabled=true, priority=1` and continue receiving routed traffic.
- **Expected**: After N consecutive auth failures, provider should auto-disable (or stay circuit-broken indefinitely until admin intervenes), since auth errors are NOT transient — retrying every N seconds will not fix anything.
- **Actual**: Standard circuit breaker hold-down (~120s) + reset, then they're tried again on the next request, fail again. Permanent waste of latency.
- **Suggested fix**:
    - In `circuit_breaker.is_billing_error()`-style classifier, add an `is_auth_error()` classifier that maps 401/403 + body-text matches to a permanent-breaker state.
    - Surface it in the UI with a red "Auth failure — re-key required" badge so admins can fix or disable it.
- **Status**: open

### BUG-003 [HIGH] OAuth access_token can be revoked server-side without local visibility

- **Area**: `app/providers/claude_oauth.py`, `_messages_streaming._complete_claude_oauth`
- **Repro**:
  1. Authorize a `claude-oauth` provider; `oauth_expires_at` = now + 8h.
  2. ~3h later, request `/v1/messages` against it → returns `401 "Invalid authentication credentials"`.
  3. `oauth_expires_at` still indicates the token is valid for ~5h more.
- **Expected**: On a 401, the proxy auto-refreshes via `refresh_and_persist()` and retries the request once.
- **Actual**: 401 propagates straight to the caller. No refresh, no retry, no failover. `oauth_expires_at` is treated as authoritative when it isn't.
- **Likely cause**: `refresh_and_persist` exists (v2.7.5) but is not wired into the request path; messages dispatch never observes the 401.
- **Suggested fix**: In `_complete_claude_oauth` and `_stream_claude_oauth`, on 401 from upstream:
  1. Call `refresh_and_persist(provider, db)`
  2. Rebuild headers with the fresh token
  3. Retry once
  4. If still 401 OR refresh fails with `invalid_grant`, return 401 to caller AND mark provider with a "needs re-auth" status surfaced in UI
- **Status**: open

### BUG-004 [MEDIUM] Brittle hardcoded version assertion in integration tests

- **Area**: `tests/integration/test_auth.py::test_health_is_public`
- **Repro**: `python3 -m pytest tests/integration/test_auth.py::TestUnauthorized::test_health_is_public`
- **Expected**: Test passes against any deployed version
- **Actual**: `assert d["version"] == "2.0.0"` — fails for every version > 2.0.0 (currently 2.7.5)
- **Fix**:
    ```python
    assert re.match(r"^\d+\.\d+\.\d+$", d["version"])
    ```
- **Status**: open

### BUG-005 [HIGH] Streaming integration tests cannot distinguish "happy path" from "upstream error"

- **Area**: `tests/integration/test_routing_mock.py::TestAnthropicStream`, `TestOpenAIStream`
- **Repro**: Run any stream test; the fixture sets up a mock provider, but the stream lands on a broken real provider that emits `{"type":"error",...}`. Tests `KeyError` on parsed events because they assume `e["type"]` is a known content event.
- **Expected**: The fixture either guarantees a working mock-only routing (no real providers in the candidate set), or the test asserts on `r.status_code != 200` first.
- **Actual**: 7 stream-related integration tests fail because of upstream provider auth errors leaking into the stream. The mock fixture's `cluster/circuit-breaker/{id}/open` calls evidently aren't enough to keep traffic off the broken anthropic providers.
- **Suggested fix**:
    - Add explicit assertion in `collect_sse` consumers that no event has `type=="error"` (fail-loud).
    - Augment `only_mock_routing` fixture: in addition to circuit-breakering, set `enabled=False` on every non-mock provider for the test scope, then restore.
- **Status**: open

### BUG-006 [MEDIUM] `_inject_claude_code_system` may break prompt caching when caller's first system block has cache_control

- **Area**: `app/api/_messages_streaming.py::_inject_claude_code_system`
- **Repro**: Caller sends `system: [{"type":"text","text":"...","cache_control":{"type":"ephemeral"}}]`.
  After injection: `system: [{"type":"text","text":"You are Claude Code..."}, {"type":"text","text":"...","cache_control":{"type":"ephemeral"}}]`.
  The caller's cached prefix changes between requests because the marker block is non-cacheable (no `cache_control`) and prepended.
- **Expected**: Caller's cache_control prefix continues to hit the cache after the proxy adds the marker.
- **Actual**: For a NEW caller (first time hitting the proxy), the prefix is now `[marker_block, user_block]` — but Anthropic's caching is keyed by content including the marker. So caching still works for repeated proxy calls, but anyone migrating from direct Anthropic API → proxy loses cache state on day 1 (different prefix).
- **Severity downgrade rationale**: caching still works for repeat traffic *through the proxy*; this is migration friction not a runtime defect. Still worth a doc note + a `cache_control` on the marker block to keep the prefix stable.
- **Suggested fix**: Add `"cache_control": {"type": "ephemeral"}` to the injected marker block so it joins the cached prefix.
- **Status**: open

### BUG-007 [LOW] OAuth refresh-token rotation pitfall easy to hit

- **Area**: `app/providers/claude_oauth_flow.py`
- **Repro**: Any caller that uses `refresh_access_token()` directly (not `refresh_and_persist()`) will consume the refresh token from the DB without writing the rotated one back. Next refresh fails with `invalid_grant` until admin re-runs the OAuth flow.
- **Mitigation in place (v2.7.5)**: `refresh_and_persist()` helper exists; live test docstring warns about the trap.
- **Open risk**: nothing prevents direct callers from grabbing `refresh_access_token` (still publicly exported). A static analysis rule or a deprecation warning would help.
- **Suggested fix**: Mark `refresh_access_token` as `_internal_refresh_access_token` (single underscore + comment) so the discoverable name is the safe one. Or have it raise unless called from `refresh_and_persist`.
- **Status**: open

### BUG-008 [HIGH] No production wiring for `refresh_and_persist` — token expiry/revocation requires admin re-auth

- **Area**: `app/api/_messages_streaming.py`, scanner.py, scheduled jobs
- **Repro**: see BUG-003 — there's no place in the request lifecycle that calls `refresh_and_persist`. The helper exists but is unused.
- **Expected paths that should call it**:
    1. `_complete_claude_oauth` and `_stream_claude_oauth`: catch 401, refresh-and-retry once.
    2. A periodic background task: every ~60min, refresh tokens whose `oauth_expires_at - now < 600s`.
    3. `scan_provider_models` and `_test_claude_oauth`: same 401 retry.
- **Status**: open

### BUG-009 [MEDIUM] Docs claim default credentials `admin/admin` but real production password differs

- **Area**: `README.md`
- **Repro**: README says "Default login: admin / admin — change immediately after first boot." Production cluster uses `REMOVED-CREDENTIAL-ROTATED-20260828` (per `tests/conftest.py`).
- **Risk**: A new admin reading the README will fail to log in and assume the system is broken; or worse, if they SQL-poke the admin row to "fix" it, they may overwrite a working password in production.
- **Suggested fix**: README should clarify "On first boot only. Change in production via the Users page; the test fixtures use `REMOVED-CREDENTIAL-ROTATED-20260828` for the existing admin."
- **Status**: open

### BUG-010 [MEDIUM] Two anthropic providers with identical priority=1 — non-deterministic routing

- **Area**: provider table / routing tiebreaker
- **Repro**: `Anthropic Claude Code #3` (anthropic, broken) and `Devin-VG` (claude-oauth, working) both have `priority=1`. LMRH ranking + CB status determines selection but the order is implementation-defined when scores tie.
- **Expected**: Either explicit tiebreaker (creation time / id ordering) or a UI warning when two enabled providers share a priority.
- **Actual**: Tiebreaker behavior is implicit (likely DB row order). Two consecutive identical requests may land on different providers.
- **Suggested fix**: When two enabled providers share `priority`, surface a yellow warning badge in the Providers UI and document the tiebreaker rule (probably `created_at` ascending).
- **Status**: open

### BUG-011 [resolved] Stale references to deleted `oauth_capture/terminal.py` or sidecar may exist

- **Area**: post-v2.7.0 cleanup
- **Repro**: `grep -rn "terminal\.py\|sidecar" app/ frontend/src/`
- **Result**: only residual *comments* found; no live code or imports. Closed as **verified-clean**.
- **Status**: verified-clean

### BUG-012 [ENHANCEMENT] Burn-test refresh path needs a "tear-down" mode

- **Area**: `scripts/test_claude_oauth_live.py`
- **Issue**: Each invocation rotates the refresh token. If anything in the rotation chain breaks, the next run fails until admin re-auths.
- **Suggested fix**: Add a `--skip-destructive` flag to `t_refresh_and_persist` so the suite can be re-run without consuming the refresh token.
- **Status**: open

### BUG-014 [MEDIUM] Activity log severity filter does literal-string match on comma-separated values

- **Area**: `/api/monitoring/activity` query handler
- **Repro**: `GET /api/monitoring/activity?severity=warning,error`
- **Expected**: returns events whose severity is `warning` OR `error`
- **Actual**: returns 0 events (matches literal column value `"warning,error"` which never exists)
- **Suggested fix**: `query.where(ActivityLog.severity.in_(severity.split(",")))` instead of `==`.
- **Status**: open

### BUG-015 [LOW] index.html served without Cache-Control

- **Area**: FastAPI SPA fallback / nginx
- **Repro**: `curl -I https://www.voipguru.org/llm-proxy2/`
- **Expected**: `Cache-Control: no-cache` (or `max-age=0, must-revalidate`) on the SPA shell so users always get the latest asset hashes after a deploy.
- **Actual**: no Cache-Control header at all. Browsers may cache index.html briefly and load stale asset hashes.
- **Suggested fix**: add `Cache-Control: no-cache` to the SPA shell response in `app/main.py` catch-all handler.
- **Status**: open

### BUG-016 [LOW] Playwright provider Test-button assertion uses stale copy

- **Area**: `tests/integration/test_playwright_ui.py::TestProviderActions::test_provider_test_button_shows_result`
- **Repro**: assertion is `span:text-matches('^OK$|^Error$')` but actual UI text is `Test OK` / `Test failed`.
- **Suggested fix**: either change the regex to `^Test (OK|failed)$` or change the badge text to a single-word `OK`/`Error`.
- **Status**: open

### BUG-017 [HIGH] No DB index on `api_keys.key_hash` — every authenticated request does a full table scan

- **Area**: schema (`app/models/db.py`)
- **Repro**: `SELECT name FROM sqlite_master WHERE type='index'` returns one row only (`ix_oauth_capture_log_capture_session`). The `api_keys.key_hash` column is the predicate on every authenticated request and has no index.
- **Expected**: `CREATE INDEX ix_api_keys_key_hash ON api_keys(key_hash)` or use `unique=True, index=True` on the column model.
- **Actual**: full scan; OK at 115 rows, painful at 10K+.
- **Severity**: HIGH not because of current pain but because it grows linearly with key count and isn't backfilled by any migration.
- **Suggested fix**: add `index=True` on `key_hash`, `provider_id` (provider_metrics, activity_log), `bucket_ts` (provider_metrics), `created_at` (activity_log), `token` (sessions).
- **Status**: open

### BUG-018 [MEDIUM] No request-level failover for non-streaming `/v1/messages` when first provider returns 401/auth-error

- **Area**: `app/api/messages.py` and `app/routing/fallback.py`
- **Repro**: send a request with the api-key configured at `priority=1` returning 401 from upstream. Proxy returns 401 to client without attempting next-priority provider.
- **Expected**: retry against next-priority capable provider, ESPECIALLY for non-billing auth errors (the request is well-formed; the provider is broken).
- **Actual**: bubbles the 401/502 out to the client.
- **Note**: this affects all provider types AND is intentionally short-circuited for `claude-oauth` (per comment in messages.py: "Claude Pro Max already runs through Claude Code's server-side routing, so we just forward..."). For OAuth this is fine when the token is good; when the token is revoked it produces user-facing 401s.
- **Suggested fix**: gated behind `settings.fallback_enabled`, retry on 401/403 against the next ranked provider once. For claude-oauth specifically, attempt `refresh_and_persist` first before failing over.
- **Status**: open

### BUG-019 [LOW] Provider creation endpoint accepts empty `api_key` for provider types that require auth

- **Area**: `POST /api/providers`, `app/api/providers.py`
- **Repro**: POST a `google` provider with `api_key=""` succeeds. The provider is enabled but every request to it 502s with `Missing Gemini API key`.
- **Expected**: validate that `provider_type in {anthropic, openai, google, vertex, grok}` requires `api_key` (or `oauth_credentials_blob`/`oauth flow` for `claude-oauth`).
- **Actual**: silently accepts empty string. Same for editing.
- **Suggested fix**: pre-flight check in `create_provider` and `update_provider`. UI may need a counterpart so admins see a clear error.
- **Status**: open

### BUG-013 [ENHANCEMENT] No version field validation across OpenAPI/health/cluster

- **Area**: release process
- **Issue**: Version strings live in `app/main.py` (5 occurrences), `app/api/cluster.py`, plus README sample, plus tests. Each release we manually `sed` them. One day someone forgets one.
- **Suggested fix**: Single source of truth — `app/__version__.py` reading `pyproject.toml` or a generated file. README sample and tests use a regex.
- **Status**: open

---

## Remediation Plan

### Tier 1 — release blockers (fix before next user-visible release)

1. **BUG-001** Streaming masks errors as 200 → 5xx-on-pre-stream-error + failover or fail-loud
2. **BUG-003** OAuth 401 not auto-refreshing → wire `refresh_and_persist` into 401-retry in both messages handlers
3. **BUG-008** `refresh_and_persist` not used in production → same wire-up as above + a periodic background refresh job for tokens approaching expiry
4. **BUG-018** No failover on auth errors → respect `settings.fallback_enabled` for 401/403 too

### Tier 2 — operator pain / data-quality

5. **BUG-002** Auth errors not classified as permanent → add `is_auth_error()` classifier; auto-disable provider after N consecutive auth failures and surface in UI
6. **BUG-017** Missing DB indexes → add migration for `api_keys.key_hash`, `activity_log.created_at`, `activity_log.provider_id`, `provider_metrics.(provider_id, bucket_ts)`, `sessions.token`
7. **BUG-014** Activity severity comma-list → `IN (...)` query
8. **BUG-019** Empty `api_key` accepted on create → preflight validation
9. **BUG-010** Two providers same priority → UI warning + documented tiebreaker

### Tier 3 — quality / hardening

10. **BUG-006** `_inject_claude_code_system` marker should carry `cache_control: ephemeral`
11. **BUG-007** Mark `refresh_access_token` as `_internal_*` to discourage direct use
12. **BUG-013** Single-source-of-truth version → `app/__version__.py`
13. **BUG-004** Test version assertion → regex
14. **BUG-005** `only_mock_routing` fixture → also disable non-mock providers, and `collect_sse` should fail-loud on `event.type=='error'`
15. **BUG-009** README admin/admin doc fix
16. **BUG-015** index.html `Cache-Control: no-cache`
17. **BUG-016** Playwright Test-button assertion → match real copy
18. **BUG-012** Burn test `--skip-destructive` flag

### Quick wins (≤30 min each)

- BUG-004, BUG-009, BUG-014, BUG-015, BUG-016 — all small textual / one-liner fixes
- BUG-006 — single-line edit
- BUG-019 — ~5 lines of validation

### Architectural fixes (need design pass)

- BUG-001 + BUG-018 — proper SSE error semantics + fallback contract for streaming
- BUG-002 + BUG-008 — provider auth-error lifecycle (classifier → CB → UI badge → auto-disable)
- BUG-017 — schema migration for indexes (and probably an alembic migration framework if not already in use)

### Recommended retest after each tier

| After Tier | Retest |
|---|---|
| 1 | Live OAuth burn test + a "deliberate broken-key" integration test (provision provider with a known-bad key, confirm: failover happens once, second 401 returns 5xx, provider transitions to disabled state) |
| 2 | DB index sanity (`PRAGMA index_list(...)`), repeat the live API key auth latency, confirm activity_log severity filter works |
| 3 | Full integration suite + Playwright; confirm version-regex test passes against any version |

---

## Last verified passing surfaces (for context)

- **Unit suite**: 633/633 passing (`python3 -m pytest tests/unit/`)
- **Cluster sync heartbeats**: 3/3 nodes healthy in last cycle
- **OpenAPI schema**: 53 paths, all have operationId
- **Provider CRUD**: roundtrip works, 404 after delete
- **Settings PUT round-trip**: persists correctly
- **RBAC**: non-admin → 403 on `/api/providers`, `/api/settings`, `/api/users`
- **Auth gate**: missing/bogus key → 401; bad password → 401
- **Rate limit**: 5/6 of 6-rapid-hits at RPM=3 → 429
- **Activity SSE stream**: emits live events
- **`refresh_and_persist` (mocked)**: 3 unit tests pass
- **`/v1/models`**: 12 models served
- **OAuth `/authorize` endpoint**: 401 unauth, 200 auth with valid PKCE URL

---

## Confirmed-fixed (kept for context)

- v2.7.1 → v2.7.2: wrong authorize URL + client_id → user-facing "error logging you in" — **fixed**
- v2.7.2 → v2.7.3: missing CC system marker → masked rate_limit_error — **fixed**
- v2.7.3 → v2.7.4: scan_models returned `[]` for claude-oauth — **fixed**
- v2.7.4 → v2.7.5: Haiku 400 with 1M-context beta + refresh-token rotation drop — **fixed**
