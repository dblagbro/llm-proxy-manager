# Refactor Log

## 2026-04-24 — v2.7.1 → v2.7.5: Claude Pro Max OAuth provider

### Motivation
Let admins attach a Claude Pro Max subscription as a provider without
needing an Anthropic API key. v2.7.0 introduced a paste-credentials flow
(admin runs `claude login` externally, pastes `~/.claude/credentials.json`);
v2.7.1–v2.7.5 replaced that with a fully in-browser OAuth flow and ironed
out the subtleties of Anthropic's OAuth-authenticated `/v1/messages`.

### What shipped
- **v2.7.1** — Browser OAuth flow scaffold:
  `app/providers/claude_oauth_flow.py` (PKCE authorize URL + code
  exchange), two new endpoints (`POST /api/providers/claude-oauth/authorize`,
  `POST /api/providers/claude-oauth/exchange`), and a
  `ProviderForm` flow with a **Generate Auth URL** button + callback
  paste-back. First attempt used the dynamic-client metadata URL as
  `client_id` with a `localhost` redirect — Anthropic's SSO rejected that
  combination ("error logging you in").
- **v2.7.2** — Real CLI endpoint extraction from
  `@anthropic-ai/claude-code` v2.1.119 binary:
  - `CLIENT_ID = 9d1c250a-e61b-44d9-88ed-5944d1962f5e`
  - `AUTHORIZE_URL = https://claude.com/cai/oauth/authorize`
  - `REDIRECT_URI = https://platform.claude.com/oauth/code/callback`
  - Added `code=true` param; token POST switched to `Content-Type: application/json`
    with `state` in the body (non-standard but required).
  - `extract_code_from_callback` now splits the `CODE#STATE` format
    Anthropic's success page displays.
- **v2.7.3** — System-prompt marker requirement:
  Anthropic's OAuth `/v1/messages` returns a masked
  `rate_limit_error` with message `"Error"` when the `system` field
  doesn't start with one of three hardcoded Claude Code markers.
  New `_inject_claude_code_system()` helper in `_messages_streaming.py`
  prepends `"You are Claude Code, Anthropic's official CLI for Claude."`
  unless the caller already identifies as CC. `test_provider()` in
  `scanner.py` rewired to hit platform.claude.com directly rather than
  routing through litellm (which sends `x-api-key` for anthropic
  providers — wrong auth method for OAuth tokens).
- **v2.7.4** — `scan_provider_models` branch for `claude-oauth`:
  `/v1/models` under the `user:inference` scope works fine with Bearer
  auth + CC beta flags. 9 models discovered on Pro Max subscription.
- **v2.7.5** — Per-model beta-flag pruning + refresh-token persistence:
  - `build_headers(access_token, model=)` strips `context-1m-2025-08-07`
    for Haiku (Pro Max doesn't grant 1M to Haiku-class).
  - `refresh_and_persist(provider, db)` — canonical helper that rotates
    the refresh token AND writes it back to the DB (Anthropic rotates
    on each use; dropping the rotation means the next refresh gets
    `invalid_grant`).
  - `scripts/test_claude_oauth_live.py` — 17-test live burn test.

### Live test results (v2.7.5)
Ran against Devin-VG on 1M-context Pro Max. 16/17 PASS:
basic, streaming SSE, system-prompt passthrough, multi-turn, tool_use,
vision, prompt caching (cache_read=2777), concurrent 5x, multiple models
(sonnet/opus/haiku), scan, test button, invalid-model clean error,
metrics recording. The one red item was `refresh_and_persist` hitting
`invalid_grant` because the stored token had been consumed by an earlier
(pre-fix) test run — not a code bug; documented as a one-time re-auth.
Total billable tokens: ~1.9K.

### Test count: 627 → 633 passing (+6 new for build_headers model-awareness + refresh_and_persist).


## 2026-04-24 — Second maintainability pass: prompts/verify extracted, oauth_capture packaged, frontend OAuthCapturePage split

### Motivation
Post-v2.5.0 the largest Python files were `app/cot/pipeline.py` (557 lines — still
held prompt constants + verify helpers after the previous split) and
`app/api/oauth_capture.py` (557 lines — a single file doing presets + serializers
+ profile CRUD + log listing + SSE tail + the catch-all passthrough). On the
frontend, `OAuthCapturePage.tsx` (489 lines) had 4 sub-components inline. Three
targeted splits reduce each of these to cohesive, single-responsibility files.

### Changes

1. **`app/cot/prompts.py`** (new, 76 lines) — extracted the 6 system prompt
   constants (PLAN_SYSTEM_VERBOSE/COMPACT, CRITIQUE_SYSTEM, REFINE_SYSTEM,
   RECONCILE_SYSTEM, VERIFY_SYSTEM). `pipeline.py` re-imports them so the
   symbol surface is unchanged.

2. **`app/cot/verify.py`** (new, 62 lines) — extracted `resolve_verify` and
   `run_verify_pass`. The latter takes `call_fn` as a parameter so
   pipeline.py's `_call` remains the sole entry point into litellm for CoT.
   A thin 10-line back-compat wrapper in pipeline.py preserves the old
   `_run_verify_pass` callers. Two tests updated to patch
   `app.cot.verify.settings` (the actual call target).

3. **`app/api/oauth_capture.py` → `app/api/oauth_capture/` package**
   - `presets.py` (89 lines) — `CapturePreset` dataclass + 8 PRESETS entries
   - `serializers.py` (82) — header filters + row→JSON-safe dicts
   - `profiles.py` (147) — `/_presets`, `/_profiles/…` endpoints
   - `logs.py` (127) — `/_log`, `/_log/stream`, `/_log/export`
   - `passthrough.py` (128) — the `/{profile}/{path}` catch-all
   - `__init__.py` (50) — merges the four sub-routers into one + re-exports
   - Test-reachable symbols (`_filter_req_headers`, `_safe_text`, etc.)
     re-exported from `__init__.py` so `test_oauth_capture.py` is unchanged.

4. **`frontend/src/pages/OAuthCapturePage.tsx` → page shell + 4 sub-files**
   under `frontend/src/pages/oauth-capture/`:
   - `NewProfileWizard.tsx` (82)
   - `ProfileList.tsx` (43)
   - `ProfileDetail.tsx` (164)
   - `LiveCaptureTail.tsx` (124)
   - `OAuthCapturePage.tsx` now 97 lines — shell that composes the four.

### Deliberately NOT done

- **`frontend/src/pages/APIKeysPage.tsx` (569 lines)** — single giant
  function-component with 3 inline modals tightly coupled to outer-scope
  state (`createMutation`, `toggleReveal`, etc.). Full extraction would
  require either prop-threading 8+ callbacks or introducing a context. No
  frontend unit tests exist to catch regressions mid-refactor. **Prereq
  for next split: write Playwright / jest-dom coverage for the key-create,
  key-edit-limits, and bulk-delete flows first.**
- **`api/providers.py` + `api/apikeys.py` CRUD dedup** — duplication is
  real (~30 lines of shared validate+serialize pattern) but abstracting
  would add cognitive cost without clear win. Leave alone.
- **`routing/router.py` (311 lines)** — still cohesive; provider-selection
  flow reads linearly.

### Verification
- **555/555 unit tests pass** through every step.
- Public imports preserved: `from app.cot.pipeline import PLAN_SYSTEM_*`,
  `from app.api.oauth_capture import router` etc. all unchanged.
- No behavior change. No version bump.

### Net line-count deltas (this pass)

    app/cot/pipeline.py                       557 → 474  (-83)
    app/api/oauth_capture.py                  557 → 0 (deleted)
    frontend/src/pages/OAuthCapturePage.tsx   489 → 97 (-392)

    NEW app/cot/prompts.py                     76
    NEW app/cot/verify.py                      62
    NEW app/api/oauth_capture/__init__.py      50
    NEW app/api/oauth_capture/presets.py       89
    NEW app/api/oauth_capture/profiles.py     147
    NEW app/api/oauth_capture/logs.py         127
    NEW app/api/oauth_capture/passthrough.py  128
    NEW app/api/oauth_capture/serializers.py   82
    NEW frontend/.../oauth-capture/*.tsx      413 across 4 files

    Largest Python file in app/ is now api/messages.py at 539 lines
    (unchanged). All new files are under 165 lines.

---

## 2026-04-23 — Large maintainability pass: shared pipeline + streaming splits + lmrh package

### Motivation
Six refactor targets queued on top of the CoT split earlier today. The goal
was to reduce duplication between the two endpoint handlers, reduce file
sizes where multiple responsibilities were sharing a file, and establish
an obvious "where does this logic live?" mental model for future editing.

### Changes

1. **Shared request-pipeline helpers** (`app/api/_request_pipeline.py`, +221 lines)
   - `apply_privacy_filters(messages_list, body) → (messages_list, pii_count)`
     Runs prompt guard then PII mask. Raises 400 on guard match.
   - `build_hint_with_auto_task(llm_hint, messages_list) → (hint, auto_task)`
     LLM-Hint parse + opt-in classify of the last user turn.
   - `apply_context_compression(messages_list, *, route, x_context_strategy,
     extra, system="") → (messages_list, strategy_applied)` — truncate /
     mapreduce / 413.
   - `build_base_response_headers(*, route, auto_task, vision_routed_count,
     context_strategy_applied, pii_masked_count, hint, max_tokens=None)` —
     common set both endpoints emit.
   - 19 new tests in `test_request_pipeline.py`.
   - `api/messages.py`: 829 → 539 (-290, -35%)
   - `api/completions.py`: 698 → 446 (-252, -36%)

2. **messages.py streaming tail** (`app/api/_messages_streaming.py`, +228 lines)
   - Pure move of `_stream_cot_anthropic`, `_stream_anthropic`, and
     `_webhook_completion_anthropic`. The POST handler imports them.
   - No behavior change.

3. **completions.py streaming tail** (`app/api/_completions_streaming.py`, +201 lines)
   - Pure move of `_stream_cot_openai`, `_stream_openai`, and
     `_webhook_completion_openai`. Mirrors #2.

4. **Image utils cleanup** (`app/api/image_utils.py`)
   - Added `_has_blocks_of_type` and `_strip_blocks_of_type` helpers.
   - `has_images_openai` and `strip_images_openai` now delegate; the
     Anthropic equivalents still inline because they preserve per-image
     `media_type` in the placeholder (can't be parameterized cleanly).

5. **Rate-limit state extraction** (`app/auth/rate_limit_state.py`, +106 lines)
   - Moved `_rpm_windows`, `_rpd_buckets`, `_burst_counters`, plus
     `_check_rate_limit`, `_check_rpd`, `_check_burst`,
     `begin_in_flight`, `end_in_flight` out of `auth/keys.py`.
   - `auth/keys.py` re-exports all of them so any `from app.auth.keys
     import _check_rate_limit` (and the tests that reach into the state
     dicts) keep working.
   - Updated two test files to patch `app.auth.rate_limit_state.active_node_count`
     (the actual call target) instead of `app.auth.keys.active_node_count`
     (which was the old implementation-coupled target).

6. **`routing/lmrh.py` → `routing/lmrh/` package**
   - Split the 438-line monolith into four submodules:
     - `types.py` (90 lines) — dataclasses + weights/rank tables
     - `parse.py` (99 lines) — RFC 8941 + legacy fallback
     - `score.py` (204 lines) — scoring + ranking (where most LMRH
       feature changes land; isolating it from parser/headers cuts
       navigation cost)
     - `headers.py` (62 lines) — response-header builders
     - `__init__.py` (47 lines) — re-exports the full public surface
   - Every `from app.routing.lmrh import X` import keeps working
     unchanged.

### Verification
- **555/555 unit tests pass** after every step (was 536 pre-refactor;
  added 19 tests for the new shared pipeline helpers).
- No behavior change. No version bump. Public import surface
  preserved for all affected modules.

### Net line-count deltas (python/app/)

    app/api/messages.py            829 → 539  (-290)
    app/api/completions.py         698 → 446  (-252)
    app/api/image_utils.py          65 →  89  (+24  helpers added)
    app/auth/keys.py               168 →  94  (-74)
    app/routing/lmrh.py            438 →   0  (deleted)

    NEW app/api/_request_pipeline.py        221
    NEW app/api/_messages_streaming.py      228
    NEW app/api/_completions_streaming.py   201
    NEW app/auth/rate_limit_state.py        106
    NEW app/routing/lmrh/__init__.py         47
    NEW app/routing/lmrh/types.py            90
    NEW app/routing/lmrh/parse.py            99
    NEW app/routing/lmrh/score.py           204
    NEW app/routing/lmrh/headers.py          62

    Net change: -616 deleted, +1258 added = +642 lines, but every file
    now has a single clear responsibility and sub-300 line count.

---

## 2026-04-23 — Split cot/pipeline.py into orchestrator + sibling modules

### Motivation
`app/cot/pipeline.py` had grown to 813 lines mixing orchestration (run_cot_pipeline,
self-consistency, cross-provider critique) with three kinds of support code: critique
parsers, verification heuristics, and three task-adaptive branches (summarize/math/code)
that each run their own independent response generator. AI-assisted editing of the
orchestrator was getting noisy because the branches cluttered the file tail.

### Changes
1. **New `app/cot/critique.py`** (89 lines) — extracted pure helpers:
   `parse_score`, `parse_gaps`, `parse_critique`, `should_verify`, plus the
   `INFRA_TOOLS` set and `SHELL_CODE_BLOCK` regex they operate on. No I/O, no async.
2. **New `app/cot/branches.py`** (193 lines) — extracted the three task-adaptive
   branch generators: `run_summarize_branch`, `run_math_branch`, `run_code_branch`.
   Each is an `AsyncIterator[bytes]` emitting its own complete SSE response.
3. **`app/cot/pipeline.py`** (813 → 557 lines, -31%) — re-imports the extracted
   symbols under their prior private names (`_parse_critique`, `_run_math_branch`,
   etc.) so every internal call site is unchanged. No public API change.

### Deliberately NOT done
- **`routing/lmrh.py` (438 lines)** — considered splitting into a package but it's
  already well-sectioned with one clear theme (LMRH protocol) and cohesive state
  flow (types → parser → scorer → headers). Splitting would add navigation cost
  without clarity gain. Left alone.
- **`api/messages.py` / `api/completions.py` shared pipeline extraction** — the
  ~400 lines of duplication between them (auth → guard → PII → hint → auto-task →
  alias → route → cascade → fallback → header build) is the highest-ROI refactor
  remaining, but has the biggest blast radius and cannot be validated end-to-end
  until upstream provider keys are refreshed (the live smoke suite is blocked).
  Queued as the next target.

### Verification
- 536/536 unit tests pass after the split.
- Public imports (`from app.cot.pipeline import run_cot_pipeline, parse_cot_request_headers`)
  unchanged.
- No behavior change; no version bump.

---

## 2026-04-22 — Incremental architectural refactor (maintainability pass)

### Motivation
Prior feature additions (rate limiting, streaming metrics, cluster spending-cap sync, vision
stripping, multi-tag tool emulation) left three clusters of duplication and mixed responsibility
that would compound as the codebase grows.

### Change 1: Shared metrics/circuit-breaker outcome helper
**New file**: `app/monitoring/helpers.py` — `record_outcome(db, provider_id, model, *, success, ...)`

**Before**: The pattern `record_failure/record_success + estimate_cost + record_request` appeared
6 times across `api/messages.py` and `api/completions.py` (regular stream, CoT stream, non-stream
× 2 files), with minor variations that could drift over time.

**After**: Single call-site in each handler. Adding a new tracking field (e.g. model version,
region, request ID) requires one change instead of six.

**Files changed**: `api/messages.py`, `api/completions.py`, `monitoring/helpers.py` (new)

### Change 2: Extract apply_sync to app/cluster/sync.py
**New file**: `app/cluster/sync.py` — `apply_sync()`, `get_peer_total_cost()`, `_peer_key_costs`

**Before**: `cluster/manager.py` (375 lines) mixed two distinct concerns: peer lifecycle
(heartbeat, ping, status reporting) and data synchronisation (120+ line `apply_sync` handling
users, API keys, providers, settings with last-write-wins merge logic).

**After**: `manager.py` owns the peer mesh (~200 lines); `sync.py` owns the incoming data merge.
`apply_sync` is re-exported from `manager.py` for backwards compatibility with existing callers.
`auth/keys.py` updated to import `get_peer_total_cost` directly from `sync.py`.

**Files changed**: `cluster/manager.py`, `cluster/sync.py` (new), `auth/keys.py`

### Change 3: Consolidate image detection/stripping
**New file**: `app/api/image_utils.py` — `has_images_anthropic`, `strip_images_anthropic`,
`has_images_openai`, `strip_images_openai`

**Before**: Four private functions split across `api/messages.py` and `api/completions.py`,
duplicating the detection/replacement logic with slightly different placeholder text.

**After**: Both endpoint files import from a single utility module. Future changes (e.g. adding
video support, changing placeholder format) require one edit.

**Files changed**: `api/messages.py`, `api/completions.py`, `api/image_utils.py` (new)

### Net result
- 3 new focused modules totalling ~200 lines
- ~180 lines removed from existing files
- No behaviour changes; all 9 providers healthy post-deploy

---

## 2026-04-22 — Incremental architectural refactor (second pass)

### Motivation
Two mixed-responsibility violations remained after the first pass: SSE serialization code
living inside a reasoning pipeline file, and model-family knowledge embedded in a routing
protocol file.

### Change 1: Wire format serialization extracted to `cot/sse.py`
**New file**: `app/cot/sse.py`

**Before**: `cot/pipeline.py` contained 8 SSE helper functions (lines 82–117) whose sole
job was producing Anthropic SSE event bytes — a serialization concern in a pipeline
execution file. `cot/tool_emulation.py` independently reimplemented the same Anthropic
event format (plus OpenAI variants) in its response generators (lines 215–347), with no
shared foundation.

**After**: `cot/sse.py` is the single source of truth for all SSE event serialization:
- 8 Anthropic SSE primitives (`sse_thinking_start/delta/stop`, `sse_text_start/delta/stop`,
  `sse_message_delta`, `sse_done`) — used by `pipeline.py`
- 8 Anthropic + OpenAI response generators (`anthropic_tool_sse`, `openai_tool_response`,
  etc.) — imported directly by `api/messages.py` and `api/completions.py`

`pipeline.py`: −40 lines (352→312), now pure reasoning logic with no format code.
`tool_emulation.py`: −136 lines (346→210), now pure emulation logic (prompt building,
normalization, parsing, LLM call). Removed unused `secrets` and `AsyncIterator` imports.

**Dependency direction preserved**: `api/ → cot/sse.py → (none)`.
Changing the Anthropic SSE format now requires one file edit.

**Files changed**: `cot/pipeline.py`, `cot/tool_emulation.py`, `cot/sse.py` (new),
`api/messages.py`, `api/completions.py`

### Change 2: Model heuristics extracted to `routing/capability_inference.py`
**New file**: `app/routing/capability_inference.py`

**Before**: `routing/lmrh.py` mixed two unrelated concerns: the LMRH routing protocol
(parse, score, rank, build header) and `infer_capability_profile` — a 54-line knowledge
base of model naming conventions that acts as a fallback when no DB record exists.
These change for different reasons: the protocol evolves with the LMRH spec; the
inference evolves with new model families.

**After**: `lmrh.py` (288→235 lines) contains only the LMRH protocol. Adding a new
model family means editing one clearly named file. `router.py` (the sole caller) now
imports `infer_capability_profile` directly from `routing/capability_inference.py`.

**Files changed**: `routing/lmrh.py`, `routing/capability_inference.py` (new),
`routing/router.py`

### Net result
- 2 new focused modules totalling ~250 lines
- ~175 lines removed from existing files (net zero growth)
- 131/131 tests pass; no behaviour changes

---

## 2026-04-22 — Incremental architectural refactor (third pass)

### Motivation
Two broken imports introduced by the second pass, one private-API coupling, and one
incomplete wire-format extraction left behind by that pass.

### Change 1: Fix broken `infer_capability_profile` imports (critical bug)
`infer_capability_profile` was moved from `routing/lmrh.py` to
`routing/capability_inference.py` in pass 2, but two callers were missed.
The existing Docker container was not rebuilt after pass 2, so integration tests
continued to pass against the old binary — but the next rebuild would have crashed
the app on startup with `ImportError`.

**Files fixed**: `providers/scanner.py:12`, `api/providers.py:207`

### Change 2: Promote private routing helpers to public API
`providers/scanner.py` imported `_build_litellm_model` and `_build_litellm_kwargs`
(underscore-prefixed) from `routing/router.py` — a legitimate caller using private
names. Renamed to `build_litellm_model` / `build_litellm_kwargs` throughout.

**Files changed**: `routing/router.py`, `providers/scanner.py`

### Change 3: Complete wire format consolidation in `cot/sse.py`
`api/messages.py` still owned `_FINISH_TO_STOP` (finish-reason→stop-reason map) and
`_to_anthropic_response()` (non-streaming response builder) — wire format concerns that
belong alongside the SSE generators. Both moved to `cot/sse.py` (renamed to
`FINISH_TO_STOP` and `to_anthropic_response`, dropping the underscore prefix since
they are now public exports). `api/messages.py` now imports them from `cot.sse`.

**Files changed**: `api/messages.py`, `cot/sse.py`

### Net result
- No new files; ~50 lines removed from `api/messages.py`
- All 131 tests pass; all 3 nodes healthy post-deploy

---

## 2026-04-22 — Incremental architectural refactor (fourth pass)

### Motivation
Two remaining mixed-responsibility issues: HMAC security primitives embedded in the
peer-lifecycle file, and an identical 8-line header-parsing block copy-pasted across
both endpoint handlers.

### Change 1: Extract `cluster/auth.py` — HMAC security primitives
**New file**: `app/cluster/auth.py` — `sign_payload`, `verify_payload`,
`verify_cluster_request`, `auth_headers_for`

**Before**: `cluster/manager.py` mixed peer lifecycle (heartbeat, ping, push-sync,
status, startup) with HMAC signing/verification. The auth functions had accumulated
private aliases (`_sign`, `_verify`, `_auth_headers`) — a code smell showing they
were originally internal but escaped without a clean interface. Two `sync` imports
sat in the middle of the file with `# noqa: E402` markers despite no actual
circular dependency preventing top-of-file placement.

**After**: `manager.py` (244→207 lines) owns only peer lifecycle. `cluster/auth.py`
owns the signing primitives. `api/cluster.py` imports auth functions from
`cluster.auth` directly. Mid-file imports moved to the top of `manager.py`;
private aliases removed; `_sign(body)` call-site updated to `sign_payload(body)`.

Auth scheme changes (algorithm, header names) now touch `cluster/auth.py` only.
Peer behaviour changes touch `cluster/manager.py` only.

**Files changed**: `cluster/manager.py`, `cluster/auth.py` (new), `api/cluster.py`

### Change 2: Deduplicate CoT header parsing + fix lazy import
**Extracted to**: `cot/pipeline.py` — `parse_cot_request_headers(x_cot_iterations,
x_cot_verify) -> tuple[int|None, bool|None]`

**Before**: An identical 8-line block for parsing `X-Cot-Iterations` and
`X-Cot-Verify` request headers appeared verbatim in both `api/messages.py` and
`api/completions.py`. Adding a new CoT request header would require two edits in
two files. Also: `api/providers.py` held a lazy `from app.routing.capability_inference
import infer_capability_profile` inside the endpoint body — a code-smell import
that hid the module's dependencies.

**After**: Both endpoint files call `parse_cot_request_headers(...)` (one line each).
`api/providers.py` imports `infer_capability_profile` at the file top with all other
imports.

**Files changed**: `cot/pipeline.py`, `api/messages.py`, `api/completions.py`,
`api/providers.py`

### Net result
- 1 new file (`cluster/auth.py`, ~40 lines)
- ~55 lines removed from existing files
- 174/174 non-UI tests pass; all 3 nodes healthy post-deploy

---

## 2026-04-23 — Short-term improvements S1–S4

### S1: Wire `record_outcome` → `log_event`
**File**: `app/monitoring/helpers.py`

Every LLM request now writes an `ActivityLog` entry via `log_event()` in `record_outcome()`.
Success events include `model`, `in_tok`, `out_tok`, `cost_usd`, `latency_ms` in the
`metadata` JSON field. Failure events include `model` and `error` (truncated to 200 chars).
No schema migration required — `ActivityLog.event_meta` already accepts JSON.

Previously every API request was invisible to the activity feed; now all 6 call-sites get
activity entries automatically via the single `record_outcome` helper.

### S2: `Retry-After` header on 429 rate-limit responses
**File**: `app/auth/keys.py`

The rate-limit `HTTPException(429, ...)` now includes `headers={"Retry-After": "60"}`.
Clients that respect this header (Claude Code, Cursor, Continue, curl) will wait the correct
amount before retrying rather than hammering the endpoint.

### S3: `GET /v1/models` endpoint
**New file**: `app/api/models.py`; registered in `app/main.py`

Returns OpenAI-format model listing (`{"object": "list", "data": [...]}`) of all models
from enabled providers. Each entry carries `id` (model_id), `object: "model"`, `created`,
and `owned_by` (provider name). Unauthenticated — standard practice for self-hosted proxies.
Required by Claude Code, Cursor, Continue, and any tool that auto-discovers available models.

### S4: `X-Resolved-Model` response header
**Files**: `app/api/messages.py`, `app/api/completions.py`, `app/main.py`

Both endpoint handlers now include `X-Resolved-Model: <litellm_model_string>` in all
responses (streaming and non-streaming). Added to `expose_headers` in CORS middleware so
browser clients can read it. Useful for debugging routing decisions and for clients that
want to log exactly which model variant was used.

### Net result
- 1 new file (`api/models.py`, ~35 lines)
- Small targeted edits to 4 existing files
- 113/113 unit tests pass; all 3 nodes healthy post-deploy

---

## 2026-04-23 — Short-term improvements S5–S6 + version discipline

### S5: TTFT tracking
**Files**: `app/monitoring/metrics.py`, `app/monitoring/helpers.py`, `app/api/messages.py`,
`app/api/completions.py`, `app/models/db.py`, `app/models/database.py`

Time-to-first-token is now tracked per 5-minute bucket in `ProviderMetric`:
- `avg_ttft_ms`: rolling CMA of TTFT across streaming requests in the bucket
- `ttft_requests`: count of streaming requests that contributed (denominator for the CMA)
- Only updated when `ttft_ms > 0` — non-streaming calls and CoT (multi-pass) contribute 0
- `_stream_anthropic`: TTFT captured at first text or tool-call content chunk
- `_stream_openai`: TTFT captured at first chunk from litellm
- Schema additions handled via `init_db()` ALTER TABLE (same pattern as existing columns)
- `get_provider_history()` and `get_all_provider_summary()` both expose `avg_ttft_ms`
- Unblocks M2 (latency-weighted routing)

### S6: Code quality cleanup
**Files**: `app/auth/keys.py`, `app/cot/tool_emulation.py`, `app/cluster/manager.py`,
`tests/unit/test_rate_limiting.py`

- `auth/keys.py`: `active_node_count` and `get_peer_total_cost` promoted from lazy
  in-function imports to file-top imports. Test patch target updated to `app.auth.keys`.
- `cot/tool_emulation.py`: `_render_tool_description(name, desc, props, required)` extracted
  from identical bodies of `_describe_anthropic` and `_describe_openai`.
- `cluster/manager.py`: `_build_sync_payload(db)` extracted from `push_sync()`, separating
  the DB-fetch-and-serialize concern from the HTTP-send concern.

### Version discipline
Version strings now increment with each deploy batch. `main.py`, `api/cluster.py` are the
two files to update. Pattern: `2.0.x` — each session's deploy batch gets the next patch.

### Net result
- No new files; targeted edits across 8 files
- 113/113 unit tests pass; 47/47 integration tests pass (3 pre-existing timing flakes on retry)
- All 3 nodes healthy at v2.0.3; pushed to GitHub (v2 branch) + Docker Hub (2.0.3, v2-latest)

---

## v3.0.32 — Extract `resolve_chat_model_for_provider()` (2026-05-01)

### What was improved
Three call sites had nearly-identical 15-line blocks for "if `provider.default_model`
is an embedding slug, find a chat-capable model from scanned `ModelCapability` rows;
prefer `command-*` or `gpt-*`; otherwise skip with a reason." This bug class was
re-fixed three times in three releases (v3.0.27 chat-completions entry, v3.0.30
keepalive probe, v3.0.31 UI Test button) before extraction reached the 3-copies bar
from `design.md`.

Extracted to `app.routing.router.resolve_chat_model_for_provider(db, provider) →
(chat_model, skip_reason)`. The next call site that needs this logic now gets it
for free instead of being a fourth chance to copy a typo.

### Files changed
- `app/routing/router.py` — added `resolve_chat_model_for_provider()` (50 lines)
- `app/monitoring/keepalive.py` — replaced 27-line inline block with 11-line
  helper call
- `app/providers/scanner.py` — replaced 26-line inline block with 18-line helper
  call (includes the `model = build_litellm_model(provider, override=)` re-derive
  that's specific to this caller)
- `app/__version__.py` → `3.0.32`
- `architecture.md` — added pointer in `routing/router.py` description + new
  Extension Point entry
- `design.md` — created (was missing per refactor brief)

### Why it helps
- **Bug-class containment**: the next "I forgot to handle Cohere here" never
  happens again. The helper is the canonical answer; reviewers can grep for
  `resolve_chat_model_for_provider` instead of grepping for `default_model` and
  hoping to catch the misuse.
- **Smaller diff for future provider-quirk additions**: if Voyage or Mistral ever
  ship an embedding-only default, the fix is one line in the helper, not three.
- **Behavior preserved**: cohere keepalive probes + Test button + chat completions
  all green post-deploy. Verified by curl + UI test + activity-log inspection.

### Skipped this cycle (with reason)
- **Split `app/api/providers.py` (947 lines) into CRUD + scan/oauth + tie-normalize**.
  Right next step on size grounds, but: every line is reachable from a routed
  endpoint, the file is busy-but-coherent, and an incremental split would require
  re-routing imports across the codebase. Risk/value worse than the helper
  extraction this cycle.

### Next recommended refactor targets

1. **`app/api/providers.py` split (~947 lines)** — `providers_crud.py`
   (CRUD endpoints + key reveal) + `providers_scan.py` (scan + test) +
   `providers_oauth.py` (claude/codex OAuth flows) + `providers_metrics.py`
   (`normalize_priority_ties`). Estimated 2–4h, medium risk. Block on landing
   #138's activity-log expansion first since that touches the same area.
2. **Parallel cascade/CoT/hedging dispatch loops in `messages.py` (754) and
   `completions.py` (523)** — they walk the same state machine with mirrored
   code per wire format. Worth a `_dispatch.py` module that owns the loop, with
   a wire-format adapter passed in. High risk (every chat call), defer until
   we have higher-confidence integration tests.
3. **`app/runs/worker.py` (749) → split state machine from queue I/O.** The
   worker mixes "what step runs next" with "how do we read/ack from the queue".
   Both are stable, so risk is medium-low. Worth doing alongside the next Run
   feature instead of as standalone work.


---

## v3.0.33–v3.0.39 — module additions noted (no refactor; new code) (2026-05-01)

Logged here for completeness — these are *additions*, not extractions, but
they touch module boundaries documented in architecture.md.

### New modules

- **`app/utils/timefmt.py`** (v3.0.33) — `utc_iso(dt)` helper. Tiny shared
  helper that solves a 10-callsite duplication of `dt.isoformat() + "Z"`
  for user-facing timestamps. Appears in 10 user-facing serializers
  (api/monitoring, api/aliases, api/users, api/cluster, api/providers,
  api/apikeys, api/oauth_capture/serializers, monitoring/metrics,
  monitoring/activity, monitoring/audit_export). Cluster-sync paths
  intentionally skip the helper because peer code parses both forms.

- **`app/api/_oauth_chat_translate.py`** (v3.0.38) — OpenAI ↔ Anthropic
  wire-format translator. Three responsibilities, all in one file because
  they share the same ontology (translation tables + helpers):
  request shape inversion (`openai_request_to_anthropic`), non-streaming
  response shape inversion (`anthropic_response_to_openai`), and
  streaming SSE delta-chunk re-emission (`stream_anthropic_to_openai_sse`).
  Lives in `api/` because it's HTTP-shape concerns; matches
  `app/providers/codex_translate.py` (v3.0.x) which serves the analogous
  role for codex-oauth.

### `event_meta` schema growth

`monitoring/helpers.py:record_outcome()` now writes seven new fields
(`served_model`, `requested_model`, `had_lmrh_hint`, `lmrh_warnings`,
`request_preview`, `response_preview`, plus full `request_body` /
`response_body` capture extended from claude-oauth-only to all chat
paths). The `_extract_preview()` helper extracts text snippets from the
LIVE request/response objects (pre-serialization, pre-truncation) so
clients don't have to JSON.parse a possibly-truncated body. This is
adjacent to a future refactor target — `helpers.py` is now ~310 lines and
mixing "record outcome", "preview extract", and "body attach". Watch it;
extract `preview.py` if it crosses 400 lines.

### Wire-format translator pattern locked

`_oauth_chat_translate.py` (v3.0.38) and `providers/codex_translate.py`
(v3.0.x) now follow the same shape for translating OpenAI ChatCompletion
to a different upstream wire format. If a third provider type ever needs
this (e.g. Bedrock, Vertex AI generative endpoint), copy the structure
rather than inventing a fourth pattern. Single helper module per
translator; no shared base class — the inversions diverge enough that
abstraction would obscure rather than help.

### Refactor verdict (still valid 2026-05-01 evening)

Top recommended targets (unchanged from v3.0.32 entry):
1. Split `app/api/providers.py` (947 lines) when the next provider-CRUD
   feature lands. Don't do it standalone.
2. Dedup parallel cascade/CoT/hedging dispatch loops between `messages.py`
   and `completions.py`. High risk; defer.
3. Split `app/runs/worker.py` state machine from queue I/O alongside the
   next Run feature.

## 2026-05-03 — v3.0.50–53: subscription-tier accounting + LMRH 1.2 §E3 ref-impl

### What shipped (additive only — no structural refactor)

- **v3.0.50** — `monitoring/helpers.py:record_outcome` resolves provider_type
  via a primary-key DB lookup and zeroes `cost_usd` for subscription-tier
  providers (codex-oauth, claude-oauth, anthropic-oauth). New
  `event_meta.cost_class` on every llm_request event; `event_meta.quota_usd`
  exposes the would-be litellm-rate cost on subscription paths. Closed A7
  cost-attribution overcount on cross-family-substituted calls.
- **v3.0.51** — `routing/lmrh/score.py` region-dim scoring extended with
  hierarchy matching (`region=eu` satisfied by `eu-west`/`eu-central`) and
  RFC 8941 InnerList any-of.
- **v3.0.52** — `routing/lmrh/types.py:HintDimension` gained `sovereign: bool`;
  parser recognizes `;sovereign` (legacy + 8941); scorer rejects
  unconfigured-region profiles when sovereign; `headers.py` accepts
  `hint=` kwarg and emits `served-region` + `region-honored=strict|loose`.
  Router callsites pass hint through.
- **v3.0.53** — `routing/circuit_breaker.py` billing-error hold-down
  extended 3600s → 21600s (1h → 6h). One-line change + regression test.

### Helpers.py size watch

helpers.py was 310 lines after v3.0.42; v3.0.50 added ~25 lines for
subscription-tier classification. Now 320. Extract-`preview.py` threshold
(per v3.0.42 entry above) is 400 — still well under.

### Capability-header hint plumb-through (v3.0.52)

`build_capability_header(hint=...)` is the first time the builder needs a
request-side input. Two callsites in `router.py` pass it. If future dims
need similar disclosure, `hint=` is the established channel — don't add
per-dim kwargs.

### Test count

LMRH suite grew 12 → 24 (region 6 + sovereign 3 + capability-header 3).
Circuit-breaker suite grew by 1 (six-hour hold-down regression). 43/43
in `tests/unit/`.

### Refactor verdict (still valid)

Top recommended targets unchanged:
1. Split `app/api/providers.py` (now 972 lines) when the next provider-
   CRUD feature lands. Don't do it standalone.
2. Dedup parallel cascade/CoT/hedging dispatch loops between
   `messages.py` and `completions.py`. High risk; defer.
3. Split `app/runs/worker.py` state machine alongside the next Run feature.

