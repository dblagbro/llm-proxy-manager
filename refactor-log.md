# Refactor Log

## 2026-06-18 — v5.7.23: completions.py + messages.py share helpers via _handler_shared (Phase 2)

Phase 2 of the operator-asked refactor proposal. Phase 1 (v5.7.18 + v5.7.19) extracted three sub-blocks from messages.py into `_messages_pre_route`. Two of those — the request setup (verify key, tenant ctx, compliance UA, emergency stop, telemetry) and body normalization (validation, suffix-strip, embedding guard, alias resolve) — were ALREADY duplicated almost verbatim in `completions.py`. Phase 2 lifts both to a new shared module, parameterized by `endpoint`.

### New module — `app/api/_handler_shared.py` (152 LOC)

`prepare_request_context(request, db, x_api_key, *, endpoint, x_conversation_id, x_memory_tag)` — keyword-only `endpoint` propagates to both `raise_if_llm_emergency_stopped` (so the "LLM stopped" 503 carries the right endpoint name in its audit row) and the Prometheus `CONVERSATION_ID_REQUESTS_TOTAL` label.

`normalize_request_body(body, x_webhook_url, db, *, endpoint)` — same five-tuple return as the Phase 1 messages-specific version. Validation helper (`validate_completion_request`) already dispatched on `endpoint`; suffix-strip + embedding guard + auto + alias are identical across endpoints.

### Files impacted

- `app/api/_handler_shared.py` (NEW, 152 LOC) — `prepare_request_context` + `normalize_request_body`
- `app/api/completions.py` (931 → 894 LOC) — two inline blocks replaced with helper calls
- `app/api/messages.py` (1063 LOC, unchanged size) — import path moved from `_messages_pre_route` to `_handler_shared`; behavior identical
- `app/api/_messages_pre_route.py` (248 LOC, unchanged) — `translate_to_openai_if_needed` stays here (messages-specific)
- `tests/unit/test_v5723_phase2_shared_helpers.py` (NEW, 10 pins)
- `tests/unit/test_v5718_messages_extract.py` — 1 pin updated to new import path
- `tests/unit/test_v5719_messages_extract.py` — 1 pin updated to new import path
- `CHANGELOG.md`, `architecture.md`, `refactor-log.md`

### Behavior preservation

82 tests pass (full v5.7.13–v5.7.23 suite). The `_handler_shared.normalize_request_body` helper passes `endpoint="completions"` from completions.py and `endpoint="messages"` from messages.py, exactly matching the prior inline `validate_completion_request(body, endpoint=...)` argument. Telemetry label, exception scope, and contextvar setup all preserve their per-endpoint identity.

### Why translate_to_openai_if_needed stays in _messages_pre_route

The Anthropic→OpenAI body translation (Phase 1 sub-block 3) only meaningful for `/v1/messages`. The `/v1/chat/completions` endpoint receives OpenAI-wire bodies natively. Putting it in `_handler_shared` would mix endpoint-specific logic into a "shared" module. Test pin `test_handler_shared_has_no_translation_helper` defends that boundary.

### Combined Phase 1+2 tally

- Phase 1 (v5.7.18 + v5.7.19): `messages.py` 1180 → 1063 (-117 LOC), `_messages_pre_route.py` 250 LOC.
- Phase 2 (v5.7.23): `completions.py` 931 → 894 (-37 LOC), `_handler_shared.py` 152 LOC.
- Net new LOC for refactor: +152 (shared) − 117 (messages.py) − 37 (completions.py) = -2 LOC overall, with CLEAR concern boundaries that future PRs can extend.

### Risks / follow-ups

- Phase 3 candidates from `docs/refactor-proposal-2026-06-17.md`:
  - `cluster/sync_handlers.py` (1118 LOC) — per-table-class split. Still deferred (cluster code, higher risk).
  - `cluster/manager.py` (1021 LOC) — cluster-management orchestration.
- `completions.py` could shed another ~50 LOC by extracting the codex-oauth bypass (line ~314) and grok-web routing (line ~429) into per-provider-type handler modules. Tabled as v5.7.24+ if traffic patterns surface a need.

---

## 2026-06-18 — v5.7.19: messages.py sub-blocks 2 + 3 extracted (Phase 1 complete)

Continuation of the Phase 1 messages.py refactor. v5.7.18 landed sub-block 1; this ship lands sub-blocks 2 and 3 together (combined because both target the same helper module).

### Sub-block 2 — request normalization

`normalize_request_body(body, x_webhook_url, db) -> (body, _orig_request_model, parsed_slug, is_auto, alias)`. Bundles the v5.0.6 original-model capture, v3.5.8 input validation, v2.8.0 suffix parsing, v3.0.27 embedding-on-chat guard, and the v2.8.0 ``model: "auto"`` resolution into a single helper. Each individual concern raises its own HTTPException at the same precondition point. The body's local rebind on suffix-strip is preserved via the helper's tuple return — caller does `body, ... = await normalize_request_body(...)`.

### Sub-block 3 — wire-format adaptation

`translate_to_openai_if_needed(*, body, route, system, messages_list, tools, has_tool_blocks, has_images) -> (body, system, messages_list, tools, translated)`. Keyword-only args force explicit call sites. The v3.10.0 widened Fix B logic (Anthropic→OpenAI translation for tool-using requests reaching any litellm-dispatched provider) moves verbatim — skip rules (claude-oauth, tool_emulation_engaged), trigger conditions (cross_family_fallback OR has_tool_blocks OR has_images OR has_anthropic_tool_defs), and the body-rebuild are all preserved. Behavioural pin in `test_v5719_messages_extract.py` confirms the skip rules fire correctly.

### Files impacted

- `app/api/_messages_pre_route.py` (105 → 250 LOC) — added `normalize_request_body` + `translate_to_openai_if_needed`
- `app/api/messages.py` (1138 → 1063 LOC) — two inline blocks replaced with helper calls
- `tests/unit/test_v5719_messages_extract.py` (NEW, 10 pins)
- `CHANGELOG.md` — v5.7.19 entry
- `architecture.md` — updated to reflect helper module growth

### Behavior preservation

56 tests pass (full v5.7.13–v5.7.19 suite). Translation helper has two behavioural pass-through pins:
- `translate_passes_through_when_not_needed` — claude-oauth route + no triggers → translated=False, inputs unchanged.
- `translate_skipped_for_tool_emulation` — tool_emulation_engaged → skip even when cross_family is true.

### Phase 1 final tally

- Pre-refactor: `messages.py` 1180 LOC, no helper extracts since v4.4.38.
- Post-Phase 1: `messages.py` 1063 LOC (-117), `_messages_pre_route.py` 250 LOC. Three concerns split: request setup, normalization, wire-format adaptation.
- Behind the 800-LOC `design.md` trigger but no longer in the worst-offender quartile.

### Risks / follow-ups

- Phase 2 (`completions.py` 931 LOC mirror) is the next pass. Many of the same concerns repeat there; `_handler_shared.py` will host the shared helpers, with thin endpoint-specific wrappers in each handler.
- Phase 2 won't fully restore symmetric structure on the first pass — some sub-blocks differ between handlers (the OpenAI Responses translation, OpenAI Chat streaming shape). Those stay handler-local.

### Next refactor targets

1. **`completions.py` 931 LOC Phase 2 mirror** — extract 3 sub-blocks + lift to `_handler_shared.py` where they duplicate messages.py.
2. **`cluster/sync_handlers.py` 1118 LOC** — per-table-class split. Still deferred until Phase 2 soak.
3. **`cluster/manager.py` 1021 LOC** — cluster-management orchestration. Still deferred.

---

## 2026-06-17 — v5.7.18: messages.py pre-route extract (Phase 1 sub-block 1)

Operator-asked refactor pass with the proposal at `docs/refactor-proposal-2026-06-17.md`. Top target by size was `app/api/messages.py` (1180 LOC vs the 800-line `design.md` trigger). The file was a single function with version-comment markers but no helper extracts since the v4.4.38 pass (when it was 861 LOC; grew +319 LOC over the next 14 v5.x ships).

### Sub-block 1 extract

Sub-block: "decide whether to even try + observability setup" — verify key, set tenant contextvar, run compliance UA pre-check (raise 451), run LLM emergency stop (raise 503), increment caller-memory telemetry counter, set caller-memory presence contextvars for the activity_log row.

Extracted to `app/api/_messages_pre_route.prepare_request_context(request, db, x_api_key, *, x_conversation_id, x_memory_tag) -> key_record`. Inline call site (50 LOC) → 6-line helper invocation. `messages.py` 1180 → 1138 LOC.

### Files impacted

- `app/api/_messages_pre_route.py` (NEW, 102 LOC) — `prepare_request_context` helper
- `app/api/messages.py` (1180 → 1138 LOC) — inline block replaced with helper call
- `tests/unit/test_v5718_messages_extract.py` (NEW, 6 source-grep + signature pins)
- `tests/unit/test_v5713-v5717` — per-ship version-bump pins relaxed to `>= ` semver checks (was breaking with every subsequent ship)
- `docs/refactor-proposal-2026-06-17.md` (NEW) — Phase 1 + Phase 2 plan, explicitly-skipped files, sign-off boundary
- `CHANGELOG.md` — v5.7.18 entry

### Behavior preservation

46 tests pass (full v5.7.13–v5.7.18 suite). The extract preserves: same module-level imports inside the helper, same call order, same HTTPException types/codes raised at the same precondition checks, same Prometheus counter labels, same contextvar set calls.

### Risks / follow-ups

- Sub-blocks 2 (request normalization: suffix-strip, embedding-on-chat guard, model:"auto" resolve, original-model capture) and 3 (cross-family fallback body rewrite + Anthropic↔OpenAI translation) are NOT in this ship — they land as follow-up patches after sub-block 1 soaks.
- Phase 2 (symmetric extract on `completions.py` 931 LOC → ~600 with shared helpers in a new `_handler_shared.py`) follows Phase 1's full completion (sub-blocks 1+2+3).
- Explicitly NOT touched: `cluster/sync_handlers.py` (1118 LOC) and `cluster/manager.py` (1021 LOC). Higher-risk replication code; deferred to a focused later pass.

### Next refactor targets (carry forward from v4.4.38, updated)

1. **`messages.py` sub-block 2 — request normalization** (~70 LOC) — next in this Phase 1 sequence.
2. **`messages.py` sub-block 3 — wire-format adaptation** (~120 LOC) — completes Phase 1.
3. **`completions.py` Phase 2 mirror** (~300 LOC of duplicate-with-messages sub-blocks → shared helpers).
4. **`cluster/sync_handlers.py` (1118 LOC)** — per-table-class split. Defer until #1+#2+#3 soak.
5. **`cluster/manager.py` (1021 LOC)** — cluster-management orchestration. Defer.
6. **`routing/router.py` (868 LOC)** — recently touched (v5.7.13 added `exclude_provider_ids`); diminishing returns after the v4.4.38 litellm-binding extract.

---

## 2026-06-02 — v4.4.38: three-target incremental refactor (router / messages / grok-web)

Post-cursor-oauth arc. The v4.4.31..v4.4.37 cursor-oauth onboarding work added 6 changes to `router.py`'s litellm-binding tables in a single week, pushing the file from 977 → 998 LOC and reinforcing what was already on the v3.10.9 "next refactor targets" list. This pass landed three behavior-preserving extracts.

### #1 — `router.py` litellm-binding extract

`PROVIDER_TYPE_TO_LITELLM`, `PROVIDER_DEFAULT_MODELS`, `build_litellm_model`, `build_litellm_kwargs`, `resolve_chat_model_for_provider`, `_is_embedding_model`, `_model_family_provider_types`, and `_native_thinking_params` moved to a new `app/routing/litellm_binding.py` (274 LOC). Re-exported from `router.py` so every existing `from app.routing.router import build_litellm_model, …` site is unchanged.

A future fifth subscription-provider type touches `litellm_binding.py` only; `router.py`'s strategy code stays untouched. `router.py` 998 → 800 LOC.

### #2 — `messages.py` cascade extract

Continued the v3.10.9 plan. v3.10.9's commit explicitly named the next target: "messages.py litellm/CoT/tool-emulation dispatch tail — extract into `_messages_dispatch.py`." This pass took the smaller, most-self-contained sub-block first — the cascade orchestration (cheap-route call → grader verdict → accept-and-return OR fall-through). Extracted as `_messages_dispatch.try_cascade_dispatch`, same shape as `dispatch_claude_oauth_chain`: returns either a `JSONResponse` (cascade accepted, caller returns immediately) or `None` (caller falls through with `resp_headers` mutated to reflect the cascade attempt).

`messages.py` 927 → 861 LOC. Still over the design.md 800-line trigger — a follow-up can extract the streaming-hedge block or the success-path tail. Incremental is the point.

### #3 — `grok_web.py` manual/bridge axis (first step)

`_bridge_chat` (the bridge-mode dispatch — POSTs to the Playwright sidecar at `Provider.extra_config.bridge_url`) moved to a new `app/providers/grok_web_bridge.py` (76 LOC). Re-exported from `grok_web.py` for back-compat (1 test imports it directly). Manual-mode HTTP replay against grok.com stays in `grok_web.py`.

First step of the full manual/bridge axial split. The follow-up converts `grok_web.py` to a `grok_web/` package with `manual.py` + `bridge.py` + `shared.py`. Doing the full package split in one session was deemed too large given the other two refactors landing in the same pass. A lazy import inside `_bridge_chat` avoids the load-time circular for the four names it borrows back from `grok_web`.

`grok_web.py` 866 → 825 LOC.

### Files impacted

- `app/routing/litellm_binding.py` (NEW, 274 LOC)
- `app/routing/router.py` (998 → 800 LOC)
- `app/api/_messages_dispatch.py` (256 → 386 LOC) — `try_cascade_dispatch` added
- `app/api/messages.py` (927 → 861 LOC) — cascade inline replaced with delegation
- `app/providers/grok_web_bridge.py` (NEW, 76 LOC)
- `app/providers/grok_web.py` (866 → 825 LOC) — `_bridge_chat` is now a re-export
- `tests/unit/test_v4438_refactor_split.py` (NEW, 8 source-guard tests pinning the three splits)
- `tests/unit/test_v380_codex_oauth_rename.py` — 1 source-grep repointed to `litellm_binding.py`
- `tests/unit/test_v3726_grok_web_status_propagation.py` — 1 source-grep widened to span both grok_web files
- `architecture.md` — module map updated for new files + split rationale

### Behavior preservation

Suite: **2441 passed + 2 skipped** (was 2433 + 2 in v4.4.37 — 8 new source-guard tests, zero regressions).

The only behavior change is a narrow edge case in the cascade extract: pre-extraction, if `record_outcome` raised inside the cascade-accept branch *after* `route = cheap_route`, the outer messages.py except attributed the failure to `cheap_route`. Post-extraction, cascade-internal success/failure is recorded against `cheap_route` inside the dispatch function; raises that propagate out of `try_cascade_dispatch` are attributed by the caller's outer except to the original `route`. No production traffic has hit this in current activity-log retention.

### Risks / follow-ups

- `litellm_binding.py` is now the canonical home for the provider-type tables. Future PRs that add a new subscription-provider type need to update both tables here (caught by the pre-existing `test_all_known_types_have_default` invariant) plus, if `base_url` is required, the api_base allowlist in `build_litellm_kwargs`. The cursor-oauth v4.4.31..v4.4.37 arc demonstrated how this triple-update can be missed; v4.4.37 added the dispatch test that pins the api_base wiring, and this refactor co-locates the three table updates so future fifth-subscription-provider PRs are one file's diff.
- `_messages_dispatch.try_cascade_dispatch` takes 11 kwargs — verbose but explicit. Same shape as v3.10.9's `dispatch_claude_oauth_chain` (13 kwargs); follow the convention.
- `grok_web_bridge.py` uses a lazy import inside `_bridge_chat` for the 4 names it borrows from `grok_web`. Hoisting that import to module top would create a load-time circular. A guard test pins this; if you need to hoist, first move the shared names to a third file (`grok_web_shared.py`) and update both sides.

### Next refactor targets

1. **`app/cluster/sync.py` (915 LOC)** — grew 97 lines since v4.4.17 (818). v4.4.24/.25 hardening + `sync_handlers` extraction in v3.9.8 already trimmed the easy parts; the remainder is the apply_sync orchestrator. Splittable along per-table handler dispatch but reads fine today. Defer unless a new table joins the sync set.
2. **`app/api/messages.py` (861 LOC)** — still over 800. Next sub-block to extract is either the streaming-hedge block (~77 LOC) or the success-path tail (record_outcome + cache store + shadow + memory write-back, ~70 LOC). The streaming-hedge extract is more cohesive.
3. **`app/providers/grok_web.py` (825 LOC)** — finish the manual/bridge split: convert to a `grok_web/` package with `manual.py` + `bridge.py` + `shared.py`. The first step (`_bridge_chat` extract) is done.
4. **`app/api/monitoring.py` (781 LOC)** — new on the watch list since v4.4.17. Hasn't been audited for distinct-concerns count yet. Skim before splitting.
5. **`app/runs/worker.py` (749 LOC)** — new on the watch list. Background job runner; single responsibility but long. Defer.
6. **`app/api/completions.py` (742 LOC)** — grew from 672 in v3.10.9. Symmetric to `messages.py`; check whether the same sub-blocks repeat and extract via shared helpers (potentially co-located in `_messages_dispatch.py`).

### Lessons learned

- **Over-large extractions backfire.** I drafted a single ~440-LOC extract for the entire non-streaming else-branch of `messages.py` (including the try-except). The resulting function had ~25 kwargs and a subtle double-record bug because the outer messages.py except still caught the HTTPException the function raised. Aborted and switched to the smaller cascade-only extract (~130 LOC, 11 kwargs, no exception-semantics change). Lesson: when an extract's kwarg count climbs past ~15 or its exception-handling semantics need a comment longer than the function's docstring, split smaller.
- **Source-grep tests are the right backstop.** Three existing source-grep tests fired during this pass (`test_router_recognizes_chatgpt_oauth_plan`, `test_all_four_raise_sites_use_map_helper`, plus the new ones I added). Each was a 30-second fix once spotted. Future refactor-log entries should call out which existing source-grep tests need repointing — saves the next session a discovery pass.
- **The v3.10.9 "next targets" comment was load-bearing.** It told me exactly what to extract from `messages.py` and let me skip the survey. Keep this section honest — it's the only thing that survives between sessions.

---

## 2026-05-15 — v3.9.15: bug-log audit refresh + BUG-007 + BUG-012

Bug-log items from the 2026-04-24 sweep were re-checked against current
code. 16 of 18 had been fixed between v2.7.6 and v3.9.14 without
updating bug-log.md statuses. This session reconciled the log + closed
the two remaining ones plus filed one newly-discovered architectural
item.

### Closed in v3.9.15

**BUG-007 — rename `refresh_access_token` → `_internal_refresh_access_token`**

Surface: `app/providers/claude_oauth_flow.py`,
`scripts/test_claude_oauth_live.py`. Discovery: the destructive primitive
had the more discoverable name; autocomplete from new callers would pick
the wrong one and silently consume the refresh token. Fix: renamed
canonical to `_internal_*`, kept old name as a one-release alias that
emits `DeprecationWarning`. Migrated the one in-tree caller (burn test)
via `as refresh_access_token` rebind so the rest of the script is
unchanged. Static-analysis test guards against re-introduction.

**BUG-012 — burn test `--skip-destructive` flag**

Surface: `scripts/test_claude_oauth_live.py`. Discovery: weekly automated
burn-test runs would rotate the live refresh token every cycle; a single
chain break left subsequent runs stuck until admin re-auth. Fix:
`argparse` flag; destructive tests record as `_record(name, True,
"skipped")` so weekly job logs a clean pass instead of a false fail.

### Filed but NOT shipped in v3.9.15

**BUG-001 — streaming-error contract** (deferred)

Status: open / deferred pending cross-team design sign-off (DevinGPT
+ hub). DevinGPT just adopted streaming write-back in v3.9.11; changing
the wire contract requires their concurrence. Two viable shapes drafted:
pre-stream errors return non-200 before the first chunk, OR post-stream
errors emit `X-Stream-Error: true` SSE header. RFC will land before
v3.10.x.

**ARCH-A — latent DB connection pool leak** (open / monitoring)

New. Surface: background workers + cluster sync. Symptoms: www01 + GCP
both saturated `QueuePool` 13h / 20h post-deploy, blocking auth on /
health 500. Audit (this session): every `AsyncSessionLocal()` is
`async with`-wrapped — leak isn't naive session management.

Hypotheses under investigation:
1. A worker using `engine.connect()` directly without context manager
2. Session held across a hung `await` (Redis/upstream timeout gap)
3. Streaming response disconnect path with leak in cleanup

Mitigations already shipped (this week, before audit):
- v3.9.8: pool stats exposed in `/health.dbPool`
- v3.9.10: Prometheus `llm_proxy_db_pool_*` gauges + 30s sampler
- v3.9.12: `tools/cut-release.sh` cuts diagnose-restart cycle time

Plan: capture `engine.pool.checkedout()` snapshot mid-event + identify
hung queries during next recurrence. Filed for action.

### Lesson learned (mostly process)

The bug-log lagged the code by ~3 weeks. Future fixes should write the
`verified-fixed in vX.Y.Z` line in bug-log.md as part of the release
ceremony — `tools/cut-release.sh` could prompt for it, or a CI check
could fail if a commit message mentions BUG-### without updating
bug-log.md. Filed as a future enhancement (not yet a ticket).

### Verification surfaces locked

- 9 new unit tests in `tests/unit/test_v3915_remaining_buglog.py`
  cover BUG-007 rename + back-compat alias + deprecation warning +
  static-analysis import guard, plus BUG-012 flag parsing + skipped-as-
  pass behavior.
- bug-log.md + qa-notes.md + refactor-log.md updated in the same
  release to keep the audit-trail honest.

---

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

## 2026-05-07 — v3.1.0: shared provider-selection + OAuth endpoint extraction

Two refactors shipped together. Both motivated by today's incident chain
(the v3.0.99 capability-filter bug + coord-hub red-dots saga) revealing
two structural smells: silent divergence between the `/v1/messages` and
`/v1/chat/completions` provider-selection blocks, plus a 1136-line
`providers.py` with two near-identical OAuth flow trios.

### Refactor 1 — shared `select_provider_with_503` + `resolve_auto_model_into_body`

Added two helpers to `app/api/_request_pipeline.py`:

- `select_provider_with_503(...)` centralizes the `select_provider`
  call + `RuntimeError → HTTPException(503)` conversion + the
  `model_override` plumbing. Both endpoints go through the same helper.
  `detailed_503=True` (default — used by `/v1/messages`) emits the
  actionable circuit-breaker / no-providers messages;
  `detailed_503=False` (used by `/v1/chat/completions`) emits the
  generic 503.
- `resolve_auto_model_into_body(body, route, is_auto)` substitutes the
  resolved model into `body["model"]` when caller used `model: "auto"`.
  Idempotent.

**Why it matters**: the v3.0.99 bug was a textbook divergence —
`/v1/chat/completions` had passed the requested model as
`model_override` since v3.0.22 (which activates router.py's family +
capability filters), but `/v1/messages` was passing `model_override=None`
when no `ModelAlias` row existed. This silently disabled the filters on
/v1/messages for ~3 weeks, force-routing gemini probes from coord-hub to
claude-oauth providers (→ 404 from platform.claude.com → red dots in
the hub UI). The fix was a one-line change in messages.py. The refactor
makes the parity **structural** — there's no separate code path to
forget to update.

**Files changed**:
- `app/api/_request_pipeline.py` (+91 lines, two new helpers)
- `app/api/messages.py` (-40 lines, ~50-line try/except block removed)
- `app/api/completions.py` (-17 lines)

**Caught regression**: first deploy 500'd on
`/v1/chat/completions + gemini-2.5-flash` because completions.py had a
stale `requested_model` reference in the `record_outcome` call path; the
variable was previously defined inline before the `select_provider`
call. Re-introduced the local right after the new helper call. ~10
minutes between regression and fix; smoke probe caught it before fleet
rollout.

### Refactor 2 — extract OAuth flow endpoints to `providers_oauth.py`

Moved 6 endpoints from `providers.py` (which had grown to 1136 lines)
to new `app/api/providers_oauth.py` (340 lines). The two flows
(claude-oauth, codex-oauth) had near-identical authorize / exchange /
rotate handlers; the only differences:

- `provider_type` column value
- Flow module name (`app.providers.claude_oauth_flow` vs
  `app.providers.codex_oauth_flow`)
- `default_model` fallback
- Which result fields get stashed in `extra_config` (codex carries
  `chatgpt_account_id` + `chatgpt_plan_type`; claude has none)

These are now captured in an `OAuthProviderSpec` dataclass with two
constants: `CLAUDE_OAUTH_SPEC` and `CODEX_OAUTH_SPEC`. Three inner
handlers (`_do_authorize`, `_do_exchange_create`, `_do_rotate`)
parameterize over the spec; the six endpoint shells are 3-line
delegations.

**Why it matters**: adding a third OAuth provider type (Vertex,
Azure-AD, Bedrock) is now ~30 lines (a flow module + a spec + three
endpoint stubs) instead of a 200-line copy-paste. The pattern is
documented in `architecture.md` under "Extension Points".

**Files changed**:
- `app/api/providers.py` (1136 → 875 lines, 23% smaller)
- `app/api/providers_oauth.py` (NEW, 340 lines)
- `app/main.py` (+2 lines: import + `include_router` registration)

OAuth helpers in providers.py (`_get_or_404`, `_stamp_user_edit`,
`_serialize`, `_bump_priority_conflicts`) stayed where they are — they're
reused by the CRUD endpoints; importing them from providers_oauth.py
uses lazy local imports to avoid a circular dependency at module load
time.

**Endpoints unchanged** — same prefix, same shape, same behavior:

```
POST /api/providers/claude-oauth/authorize
POST /api/providers/claude-oauth/exchange
POST /api/providers/{id}/oauth-rotate
POST /api/providers/codex-oauth/authorize
POST /api/providers/codex-oauth/exchange
POST /api/providers/{id}/codex-oauth-rotate
```

### Test impact

904/904 unit tests still pass. Pure structural change — no behavior
modification. Live smoke on www01: `POST /v1/messages` + claude/gemini/
gpt → 200; `POST /v1/chat/completions` + claude/gemini/gpt → 200.
All 6 OAuth routes register correctly per `/openapi.json`.

### File-size deltas

| File | Before | After | Δ |
|------|--------|-------|---|
| `providers.py` | 1136 | 875 | -261 |
| `providers_oauth.py` | (new) | 340 | +340 |
| `_request_pipeline.py` | 221 | 312 | +91 |
| `messages.py` | 844 | 804 | -40 |
| `completions.py` | 639 | 622 | -17 |
| **Net** | **2840** | **2953** | **+113** |

Net line growth of +113 — but that includes a new module header,
docstrings, and the parameterizing dataclass. Actual duplicated logic
removed: ~250 lines (OAuth) + ~50 lines (provider-selection) = 300.
Trade is worth it for divergence-prevention + extension-point clarity.

### Refactor verdict (updated)

Top recommended targets:
1. `app/runs/worker.py` (749 lines) — split state machine alongside the
   next Run feature. Still defer.
2. Dedup the cascade/CoT/hedging dispatch loops between `messages.py`
   (now 804 lines) and `completions.py` (622). Still HIGH RISK; defer
   until either both files shrink further or a feature naturally
   requires unification.
3. `providers.py` capability/scan-models block (~150 lines around
   `scan_models` + `list_capabilities` + `put-capability`) — could
   become `providers_capabilities.py` if a future scan/infer feature
   lands. Not urgent.
4. NEW: `app/api/_messages_streaming.py` (701 lines) houses 5 fns —
   `_stream_cot_anthropic`, `_stream_anthropic`, `_stream_claude_oauth`,
   `_complete_claude_oauth`, `_webhook_completion_anthropic`.
   Splittable into `_litellm_dispatch_anthropic.py` (litellm path) +
   `_claude_oauth_dispatch.py` (direct platform.claude.com path) if the
   next OAuth-direct provider type lands. Not urgent.

## 2026-05-07 — v3.1.1 + v3.1.2: test fixture hardening + bulk catalog cluster-sync

Two follow-ups to today's incident chain. Operational hardening rather than
structural refactoring, but logged here because both touch sync/cluster
paths and update the next-targets list.

### v3.1.1 — Test fixture hard-purge endpoint + sessionfinish hook

Closed the test-tombstone leak that produced the 127 stale `pytest-*` /
`test-playwright-*` / `debug-*` rows I cleaned up in cycle 3.

**Root cause**: `tests/integration/test_playwright_ui.py:test_create_api_key_flow`
hardcoded the name `test-playwright-key` and never deleted it. Each CI run
leaked one row; eventually a previous bulk-cleanup pass soft-deleted them
all at the same instant (`2026-05-01 00:10:34.547017`), giving 127
identical-timestamp tombstones. The 7-day cluster-sync tombstone retention
meant they sat in every apply_sync pass, contributing to the v3.0.96 →
v3.0.98 latency cascade.

**Fix**:
- `app/api/apikeys.py`: new admin-only endpoint
  `POST /api/keys/_purge-test-tombstones`. Hard-deletes tombstoned api_keys
  whose `name` matches a test pattern AND whose `deleted_at` is older than
  60s (cluster-sync convergence buffer).
- `tests/conftest.py`: new `pytest_sessionfinish` hook calls the endpoint
  after every test session. Best-effort.
- `tests/integration/test_playwright_ui.py`: `test_create_api_key_flow`
  now uses `test-playwright-{uuid}` + `try/finally` cleanup that calls
  the standard DELETE endpoint. The sessionfinish hook is the safety net.

### v3.1.2 — Bulk catalog cluster-sync (re-enables `cluster_sync_catalog_tables` default)

The proper rework of the v3.0.96 catalog-sync apply path. v3.0.98's hotfix
disabled the feature entirely; v3.1.2 fixes the apply path so it can run
safely.

**Old path** (per-row `SELECT` then `INSERT/UPDATE` for each `ModelCapability`
row): with 304 rows × DB round-trip = 12-17s per sync, DB ~50% contended,
real `/v1/messages` calls queued past nginx's 60s upstream timeout.

**Investigated `INSERT … ON CONFLICT DO UPDATE`** first. Rejected because
`ModelCapability`'s primary key is an autoincrement `id`, with no composite
UNIQUE on `(provider_id, model_id)`. ON CONFLICT requires a constraint to
conflict against; adding one would need a schema migration with
dup-detection — overkill for this win.

**Final approach** (file: `app/cluster/sync.py`):
1. Filter incoming rows by FK (skip orphan caps whose Provider hasn't
   replicated yet) and collect `(provider_id, model_id)` keys.
2. ONE bulk SELECT pulls all existing rows whose composite key matches
   any incoming row. SQLAlchemy `tuple_().in_()` compiles to a single
   `WHERE (provider_id, model_id) IN ((…))` query on SQLite.
3. Iterate incoming rows in-memory using the pre-fetched index. New
   rows go through `db.add()`, existing rows mutate the loaded ORM
   instance with the same per-row LWW logic as before.
4. Single commit at the end flushes all inserts and updates.

The semantics are unchanged from the v3.0.96 per-row code; only the DB
round-trip count drops from O(N) to O(1) plus a single batched commit.

**Benchmark (304-row payload on www01)**:
- First sync after enable: ~2s (one-time apply of all 304 rows where
  peer_updated > local)
- Steady-state apply: 48-52ms (LWW short-circuit when stamps match)

**Live deploy verification**:
- All 4 nodes confirmed on v3.1.2
- Sync latency: p50=106-162ms, p95=109-169ms (vs 12-17s pre-rework, 200-919ms
  with feature disabled)
- Cross-node convergence within first cycle: www02 jumped from ~0 to 295
  caps; www01 has 304 because 9 of its rows are orphan caps for a deleted
  provider (`e5e3905b79d1`) that the FK pre-filter correctly refuses to
  materialize on peers.

**Default re-enabled** (`config.py: cluster_sync_catalog_tables=True`).
Without this, ModelCapability discoveries on one node never reach peers
and `/v1/models` capability scoring drifts.

**Files changed**:
- `app/cluster/sync.py` (-15 lines net: per-row block replaced with bulk
  block; total in apply_sync ~unchanged due to verbose comments)
- `app/config.py` (+1 line: default flipped)
- `app/api/apikeys.py` (+40 lines for new endpoint)
- `tests/conftest.py` (+25 lines for sessionfinish hook)
- `tests/integration/test_playwright_ui.py` (+15/-5 lines for the leak fix)

### Refactor verdict (updated)

Top recommended targets:
1. `app/runs/worker.py` (749 lines) — defer to next Run feature.
2. Cascade/CoT/hedging dispatch dedup between messages.py + completions.py.
   HIGH RISK; defer.
3. Tombstone propagation for catalog tables (ModelCapability, ModelAlias,
   OAuthCaptureProfile). v3.0.97 added `deleted_at` columns; v3.1.2 doesn't
   yet honor them in the build/apply paths. Currently if an admin deletes
   a ModelCapability row on www01, peers don't learn and may resurrect the
   row from their own scans. Low urgency — most admins don't delete
   capability rows manually. Add to bulk apply when next operator
   workflow needs deletion-by-name to propagate.
4. Cleanup of orphan ModelCapability rows on www01 (9 caps for deleted
   provider `e5e3905b79d1`). Should be a one-off `DELETE FROM
   model_capabilities WHERE provider_id NOT IN (SELECT id FROM providers)`
   admin sweep, or a scheduled GC job. Not urgent — they don't break
   anything, just sit in the table.
5. NEW: `app/api/_messages_streaming.py` (701 lines) — splittable when
   next OAuth-direct provider type lands. Not urgent.


---

## 2026-05-08/09 — v3.2.0–v3.2.8: grok-web provider + Playwright bridge sidecar

### Scope

Eight versions across two days adding a third "subscription as a
provider" path (after `claude-oauth` and `codex-oauth`): `grok-web`.
Operators can now bring their grok.com Lite/Premium subscription into
the proxy via cookie replay (manual mode) or a Playwright sidecar that
maintains a live logged-in browser session (bridge mode).

### Key changes

**v3.2.0 — `grok-web` provider type (cookie replay)**
- New `app/providers/grok_web.py` — manual-mode dispatcher: HTTP replay
  against `https://grok.com/rest/app-chat/conversations/{id}/responses`
  using cookies + headers stored on `Provider.extra_config`.
- Wired into `app/api/messages.py` (Anthropic) and
  `app/api/completions.py` (OpenAI) with parallel ~50-line dispatch
  blocks. Both surfaces stream + non-stream. Models: grok-3 (fast),
  grok-4 (expert).
- `app/routing/router.py` family filter extended:
  `x-ai/*` → `{grok, grok-web, openrouter}`.
- `app/providers/scanner.py` static catalog for grok-web (no upstream
  model-list API; SUPPORTED_MODELS hardcoded).
- Frontend `ProviderForm` extended with cookie/conv_id paste fields.

**v3.2.1 — bridge mode in dispatcher**
- `_is_bridge_mode(extra_config)` switches on `bridge_url`. When set,
  `complete_grok_web` / `stream_grok_web` forward the request to the
  bridge sidecar's `/api/chat` instead of replaying locally.
- Bridge holds the cookies; dispatcher just shapes the request and
  proxies the response.

**v3.2.1+ — `grok_bridge/` sidecar package**
- New top-level Docker service `llm-proxy2-grok-bridge`. Image based on
  `mcr.microsoft.com/playwright/python:v1.45.0-jammy` + xvfb + x11vnc +
  websockify + noVNC + a small FastAPI control plane.
- Persistent state volume `/data/playwright-state` survives container
  restarts (cookies + localStorage held in Playwright user_data_dir).
- Background loop visits grok.com every 25 min so Cloudflare passively
  reissues `__cf_bm` / `cf_clearance`.
- Operator signs in once via noVNC at `/grok-bridge/login`; bridge
  thereafter serves /api/chat indefinitely.

**v3.2.2 — wizard UI**
- `GrokWebProviderFields` component (extracted from inline `ProviderForm`
  block) — Bridge / Manual tabs, live status poll against `/api/status`,
  "Connect Grok" button that opens noVNC.

**v3.2.3 — backend validator dual-mode**
- `app/api/providers.py` accepts either `bridge_url + conversation_id`
  (bridge mode) or `cookie_header + conversation_id` (manual). Error
  messages updated to nudge operators toward Bridge mode.

**v3.2.4 — wizard auto-populate fix**
- `useEffect` on mount injects `bridge_url` + `bridge_token` defaults
  into form state when in Bridge mode. Pre-fix: tab visually selected
  but defaults only fired on click → form submitted with empty fields
  → backend rejected.

**v3.2.5 — bridge boot UX + conversation_id auto-detect**
- Bridge lifespan navigates to grok.com on startup (not about:blank).
- `/api/status` surfaces `current_conversation_id` parsed from the
  bridge's current page URL; wizard shows "Use bridge's current" button.

**v3.2.6 — cross-node bridge access**
- `/grok-bridge/api/chat` removed from nginx auth_request so peer
  llm-proxy2 nodes (www02, smoke, GCP) can call the bridge via the
  public URL `https://www.voipguru.org/grok-bridge/api/chat`. Auth on
  this surface is X-Bridge-Token, enforced inside the bridge container.
- Other /grok-bridge/* paths remain admin-session gated.

**v3.2.7 — cluster-sync LWW tie-break fall-through**
- `app/cluster/sync.py` apply_sync: when peer and local
  `last_user_edit_at` are equal (real tie, not "missing stamp"), fall
  through to legacy LWW on `updated_at` with strict-greater. Catches
  background mutations (direct DB writes, sync-cascade flushes) that
  bumped only `updated_at` without touching `last_user_edit_at`.
- `_parse_iso` returns naive UTC so peer (tz-aware) vs local (tz-naive)
  comparisons no longer TypeError silently.
- `tests/unit/test_cluster_sync_lww.py` (4 cases): strict-greater
  anti-ping-pong preserved; tie + newer updated_at accepts;
  peer-newer-user-edit accepts; peer-older-user-edit rejects-even-with-
  newer-updated_at.

**v3.2.8 — capability slug aliases**
- `SUPPORTED_MODELS` extended with `x-ai/grok-3` + `x-ai/grok-4` so
  caller-side OpenRouter-style slugs match grok-web's capability rows.
  Pre-fix: 6 of 8 grok requests in 24h routed to OpenRouter (per-call
  billing) despite grok-web at priority=1, because router scores by
  exact capability match. Post-fix: `X-Resolved-Provider: grok-web`
  for x-ai/grok-4 verified live.

### Architectural decisions

- **Sidecar over in-process Playwright**: keeps llm-proxy2's image
  small (Playwright + Chromium adds ~1 GB) and isolates the long-lived
  browser session from request-handler restarts. Inference path is one
  HTTP hop slower (~50ms) — acceptable trade for clean separation.

- **Cookie persistence on Docker volume**: the obvious alternative was
  storing tokens in `Provider.oauth_*` columns à la claude-oauth.
  Rejected because Cloudflare cookies are anti-bot signals tied to TLS
  fingerprint + IP + browser context — they don't survive an
  HTTP-replay rehydration the way OAuth bearer tokens do. The bridge's
  Chromium IS the trust context.

- **Public bridge URL with token auth (not docker-internal hostname)**:
  v3.2.6 rejected putting peer-side bridge_url on a docker-network
  hostname because peers' docker stacks are independent. Public URL +
  X-Bridge-Token is one extra TLS hop per call (~50ms) — acceptable.

- **Single conversation_id vs auto-create**: `/conversations/new` is
  Cloudflare-rejected from server IPs even with fresh cookies. Could be
  worked around by driving Playwright in-browser fetch (TLS fingerprint
  matches Chromium); deferred as a v3.2.9+ enhancement.

### Files impacted

**Backend:**
- `app/providers/grok_web.py` (new, 743 lines)
- `app/api/providers.py` (+30 lines validator + tombstone of test record)
- `app/api/messages.py` (+72 lines grok-web dispatch block)
- `app/api/completions.py` (+50 lines grok-web dispatch block)
- `app/routing/router.py` (+10 lines: x-ai/ family + PROVIDER_TYPE_TO_LITELLM)
- `app/routing/capability_inference.py` (+1 line)
- `app/providers/scanner.py` (+25 lines: SUPPORTED_MODELS branch + smoke test)
- `app/cluster/sync.py` (+15 lines: tie-break fall-through, _parse_iso normalize)
- `app/__version__.py` (8 bumps)

**Bridge (new):**
- `grok_bridge/Dockerfile` (~50 lines)
- `grok_bridge/supervisord.conf` (~50 lines)
- `grok_bridge/start.sh` (~30 lines)
- `grok_bridge/app.py` (~580 lines)

**Frontend:**
- `frontend/src/components/providers/GrokWebProviderFields.tsx` (new, ~280 lines)
- `frontend/src/components/providers/ProviderForm.tsx` (+10 lines: import + render branch)
- `frontend/src/types/index.ts` (+4 lines)

**Infra:**
- `/home/dblagbro/docker/docker-compose.yml` (+15 lines: bridge service + volume)
- `/home/dblagbro/docker/config/nginx/nginx.conf` (+50 lines: 4 location blocks + auth_request)

**Tests:**
- `tests/unit/test_cluster_sync_lww.py` (new, 4 cases)

### Risks

- **`grok_web.py` at 743 lines** is now the second-largest file in
  `app/`. Manual-mode and bridge-mode paths share helpers but each has
  its own `complete_*` / `stream_*` shape. A small extraction pass
  (Phase B of the May 2026 doc-catch-up) is queued.
- **Dispatch duplication** between `app/api/messages.py` and
  `app/api/completions.py` (~50 lines each, similar shape). Same Phase
  B target.
- **`grok_bridge/app.py` re-implements** `_flatten_messages_to_prompt`,
  `_build_grok_body`, `_model_to_mode_id` from `app/providers/grok_web.py`.
  These are independent codebases (separate Docker images), so the
  duplication is intentional — but they can drift. A shared schema or
  tested-contract approach is the long-term answer.
- **Test gap**: only v3.2.7 added unit tests. v3.2.0–v3.2.6 + v3.2.8
  shipped without coverage. Phase C of the doc-catch-up adds them.
- **Bridge SPOF for grok-web**: only www01 runs the bridge container;
  if it dies, all 4 nodes lose grok-web routing. Acceptable for a
  single-subscription provider but worth flagging.

### Remaining issues / next refactor targets

1. **Phase B**: extract `app/api/_grok_web_dispatch.py` shared by
   messages.py + completions.py; consolidate manual+bridge branches in
   `grok_web.py`.
2. **Phase C**: regression tests for manual-mode dispatcher, bridge-mode
   dispatcher, x-ai/ slug routing, budget cap enforcement, bridge
   `/api/chat` contract.
3. **`POST /conversations/new` via Playwright fetch** — bypasses
   Cloudflare anti-bot by using the live Chromium TLS context. Removes
   the "operator provides one conversation_id" friction.
4. **Last-user-edit-at hardening**: SQLAlchemy event listener that
   bumps the stamp on user-editable field changes. Prevents the
   v3.2.7 class of bug from recurring when admin scripts touch the DB
   directly.
5. **Activity log api_key_id orphans** (8 events with deleted-key
   refs). Minor; ages out via 30-day prune.

---

## 2026-05-09 — v3.2.10–v3.3.1: grok-web observability + LMRHv2 protocol

### Scope

Six versions in one session continuing the v3.2.x grok-web sprint and
opening the v3.3.x LMRHv2 protocol family. Three buckets:

1. **Observability fixes** for the v3.2.0 grok-web that had been
   shipping invisibly (no metrics, no probes).
2. **Cluster-sync hardening** to prevent the v3.2.7 ad-hoc fix from
   needing repeat application.
3. **LMRHv2 Phase 1 + 2** — bidirectional metrics feedback channel,
   designed and shipped in one session after operator approved all
   7 design questions.

### Key changes

**v3.2.10** — observability bug fixes:
- `app/api/_grok_web_dispatch.py` now plumbs `record_outcome` on every
  terminal state. Streaming wrappers count chars for token estimates.
- `app/monitoring/keepalive.py` adds `grok-web` to `SUBSCRIPTION_TYPES`
  + new `_probe_one` branch dispatching via `complete_grok_web`.
- `app/monitoring/helpers.py` adds `grok-web` to
  `SUBSCRIPTION_TIER_PROVIDER_TYPES` so cost-class stays subscription.

**v3.2.11** — Playwright `/conversations/new` + auto-stamp listener:
- `grok_bridge/app.py` /api/conversation/new uses Playwright Locator
  API (auto-retries on stale DOM) to drive the SPA rather than the
  blocked server-side POST. In-browser `fetch()` confirmed still 403'd
  by Cloudflare → URL pattern is the gate, not TLS fingerprint.
- New `app/models/_user_edit_stamp.py` registers a SQLAlchemy
  `before_update` listener that auto-bumps
  `Provider.last_user_edit_at` on user-meaningful column changes.
  Excludes background-rotation columns. Belt-and-suspenders for the
  v3.2.7 fix.

**v3.2.12** — `api_key_prefix` denormalized into `event_meta`:
- `record_outcome` looks up `ApiKey.key_prefix` once per event and
  writes it. Probes get the literal `"probe-keepalive"` string. Fixes
  the proactive-monitoring sweep's mis-attribution.

**v3.3.0** — LMRHv2 Phase 1:
- New `app/routing/lmrh/snapshot.py` (~340 lines) — in-memory snapshot
  with 30 s background refresh loop. Frozen dataclasses + per-key
  scope filter. ETag derivation from identity-affecting fields
  (excludes `as_of`).
- New `app/api/lmrh_v2.py` (~330 lines) — endpoint router with
  per-key sliding-window rate limit. `Cache-Control` + ETag
  conditional GET. Feature-flag gate (`lmrh_v2_enabled`).
- `app/main.py` — register router, start snapshot loop, add Link
  header injection middleware (RFC 8288).
- `app/models/db.py` — `ApiKey.lmrh_polling_rpm` + `lmrh_quotes_rpm`
  override columns.
- `app/config.py` + `app/config_runtime.py` — `lmrh_v2_enabled`
  setting, default False.

**v3.3.1** — LMRHv2 Phase 2:
- `app/routing/router.py` — `select_provider(dry_run=True)` returns
  the ranked candidate list (provider, profile, unmet, score) before
  winner-pick + dispatch. ~20 lines added.
- `app/api/lmrh_v2.py` — new `GET /lmrh/quotes` endpoint joins
  ranked candidates with snapshot metrics for predicted-cost /
  predicted-latency rendering.
- New `sdk/python/lmrh_client.py` (~370 lines) — single-file Python
  reference SDK. httpx-based polling thread, ETag-aware, graceful 404
  degradation. `build_hint(prefer=...)` synthesizes valid LMRH 1.x
  hints from caller preferences.

### Architectural decisions

- **Per-cluster vs per-node feature flag**: `lmrh_v2_enabled` lives in
  the cluster-synced `SystemSetting` table, so flipping on one node
  propagates to peers. Operator decision #6 said "per-node flip" but
  practically this becomes per-cluster. Acceptable for LMRHv2 (the
  protocol is read-only + additive, so fleet-wide enable is safe).
  Future flags that need true per-node control will need a separate
  config path (env-var-only, no SystemSetting row).

- **Per-node snapshot, no cluster sync of the snapshot itself**: each
  proxy node builds its own snapshot from its local ProviderMetric.
  The underlying ProviderMetric IS already cluster-replicated, so the
  snapshots converge. Cheaper than syncing the rendered snapshot
  every 30 s.

- **Phase 2 reuses `select_provider` with a `dry_run` flag** rather
  than extracting a helper. Reasoning: the function already encodes
  a non-trivial filter pipeline (10+ filters: enabled, pinned,
  exclude_id, ownership, available, tools, family, model_supports,
  embedding-only, etc.). Extracting would risk subtle divergence
  between dry-run and real-dispatch. Single function, single source
  of truth, clean opt-in.

- **SDK ships with the proxy repo, not as a separate package**.
  Operators vendor it via `cp` for now; if downstream usage proves
  out, we publish to PyPI. Avoids npm-style dependency hell across
  the bot fleet.

### Files impacted

**Backend**:
- `app/api/_grok_web_dispatch.py` (~530 lines after v3.2.10 hardening)
- `app/api/lmrh_v2.py` (NEW, ~480 lines after v3.3.1)
- `app/api/messages.py` + `app/api/completions.py` (+5 line dispatch
  call sites pass observability args)
- `app/monitoring/keepalive.py` (+30 lines grok-web branch)
- `app/monitoring/helpers.py` (+15 lines)
- `app/routing/router.py` (+25 lines dry-run mode)
- `app/routing/lmrh/snapshot.py` (NEW)
- `app/models/db.py` (+2 columns on ApiKey)
- `app/models/_user_edit_stamp.py` (NEW, ~80 lines)
- `app/models/database.py` (+3 ALTER TABLE)
- `app/main.py` (+15 lines: link header middleware, snapshot start)
- `app/config.py` + `app/config_runtime.py` (+lmrh_v2_enabled)

**Bridge**:
- `grok_bridge/app.py` (~50 lines for /api/conversation/new rewrite)

**Frontend**:
- `frontend/src/components/providers/GrokWebProviderFields.tsx`
  (+30 lines for "Create new" button)

**SDK** (new):
- `sdk/python/lmrh_client.py` (NEW, ~370 lines)
- `sdk/python/test_lmrh_client.py` (NEW, 11 tests)
- `sdk/python/README.md` (NEW)

**Tests**:
- `tests/unit/test_user_edit_stamp.py` (NEW, 7 tests)
- `tests/unit/test_record_outcome_meta.py` (NEW, 4 tests)
- `tests/unit/test_lmrh_v2.py` (NEW, 13 tests)
- Existing files updated for new behavior

**Net test count**: 950 → 988 (977 unit + 11 SDK).

### Risks

- **`app/api/_grok_web_dispatch.py` at 530 lines** is now the largest
  single dispatcher in the codebase. Phase 1 split it OUT of
  messages.py + completions.py (good); Phase 2's record_outcome
  plumbing added another ~100 lines. Splittable along the
  manual/bridge axis if it grows further.
- **`app/api/lmrh_v2.py` at 480 lines** has 5 endpoints + rate
  limiter + snapshot rendering. Splittable when /lmrh/quotes grows
  scoring features (e.g. cost-class explanation) — for now, single
  file is the right cohesion.
- **In-memory rate-limit state** (`_rate_state`) is per-process. A
  caller hitting both www01 and www02 sees its rate-limit budget
  doubled. Acceptable for the v3.3.0 default budgets (4/min); when
  we tighten budgets or scale up callers, move to Redis.

### Remaining issues / next refactor targets

1. **LMRHv2 Phase 4** (subscription-quota disclosure) — operator
   approved in v3.3.0 design but deferred to a future ship. Wire
   `usage_session_window_sec` etc. into snapshot rendering, gated by
   ownership filter.
2. **SDK adoption** — pick one downstream caller (likely
   coordinator-hub or DevinGPT) to integrate the SDK and validate the
   API shape before publishing to PyPI.
3. **Activity-log api_key_id orphan cleanup** — recurring item;
   ages out via 30-day prune.
4. **`coordinator-post` jq fix propagation** — local fix to the
   `--arg label` reserved-keyword collision needs to be pushed to
   other bots via the coordinator installer.

---

## v3.10.9 — extract the claude-oauth dispatch chain from messages.py

`messages.py`'s `messages()` handler had grown into a ~913-line
function (file: 1002 lines) — well past design.md's 800-line /
>5-concern split trigger, and it is the hot path every `/v1/messages`
feature touches. Its deepest, gnarliest branch was the claude-oauth
provider-chain walk: streaming vs non-streaming dispatch, a 401/403
refresh-then-fallback that re-selects a provider and may re-enter the
loop, a network-error fallback, and the success path's cache-disclosure
+ quality-hint + memory write-back.

That branch (~170 lines) plus its chain-walk helper `_select_excluding`
were extracted into a new `app/api/_messages_dispatch.py` as
`dispatch_claude_oauth_chain()`. The function returns `(response, route)`:
a non-None response means the request was served and the caller returns
it as-is; None means the chain is exhausted and `route` now points at a
non-claude-oauth provider, so the caller falls through to the litellm
path. Pure behavior-preserving move — the dispatch logic is unchanged,
only the `return X` statements gained `, route` and the five in-loop
lazy imports were hoisted to the module top.

`_messages_dispatch.py` is the sibling of `_messages_streaming.py`: the
latter holds the SSE *generators* (`_stream_claude_oauth` etc.), the
former holds the *orchestration* that drives them. `messages.py` now
delegates with a 13-line call site. Result: `messages.py` 1002 → 816
lines; the `messages()` mega-function shed its hardest branch.

Also collapsed the duplicate `docs/architecture.md` — a stale v3.7.13
copy — to a one-line pointer at the canonical root `architecture.md`.
One architecture document, no drift trap.

### Files impacted
- `app/api/_messages_dispatch.py` (NEW, 256 lines) — `dispatch_claude_oauth_chain` + `_select_excluding`
- `app/api/messages.py` (1002 → 816) — claude-oauth block + `_select_excluding` removed; 13-line delegation added
- `docs/architecture.md` — collapsed to a pointer
- `architecture.md` — module map updated
- `tests/unit/test_v3109_messages_dispatch_extract.py` (NEW, 4 tests)
- `tests/unit/test_v3911_streaming_memory_writeback.py` — one source-grep test repointed to the new file

### Risks
- `messages.py` at 816 is still marginally over the 800 trigger. The
  remaining oversized concern is the CoT / tool-emulation / litellm
  dispatch tail. Extract it next — into the same `_messages_dispatch.py`
  — to bring `messages.py` comfortably under 800. Kept incremental so
  each move's behavior preservation stays verifiable.

### Remaining issues / next refactor targets
1. **messages.py litellm/CoT/tool-emulation dispatch tail** — extract
   into `_messages_dispatch.py` (the obvious next pass).
2. **completions.py (672 lines)** — symmetric to messages.py; check for
   the same dispatch-orchestration shape and extract if present.
3. **grok_web.py (866, documented as 743)** — split along the
   manual/bridge axis.
4. **providers.py (958)** — over 800; CRUD-heavy, lower urgency.

### Tests
1969 unit tests pass (4 new in `test_v3109`; 1 repointed). Behavior
preserved — verified by the full suite plus the cross-family
translation integration suite that exercises `/v1/messages`.

---

## v5.0.9 (2026-06-04) — extract `_compliance_handler.py` from messages.py + completions.py

### Why

The v5.0.0–v5.0.7 compliance ship duplicated four orchestration sites
between `app/api/messages.py` and `app/api/completions.py`:

1. UA pre-check (~50 lines verbatim in both)
2. `ComplianceNoSubstituteError` / `ComplianceNoLocalProviderError`
   → 503 conversion (~60 lines)
3. Substitution disclosure setup + `emit_event` on 200 OK (~40 lines)
4. Upstream-error 502 follow-up `emit_event` + header merge (~70 lines)

Every v5.0.x patch (v5.0.1 / v5.0.2 / v5.0.4 / v5.0.6 / v5.0.7) touched
both files in lockstep. The v5.0.4 F-anomaly fix had to add a new
exception subclass + catch block in both. The v5.0.6 audit-field fix
had to update 8 emit_event call sites across both. Per design.md the
"three callers" rule is comfortably exceeded — we're at 5+ touches in
24 hours with one near-verbatim mirror.

### What

Extracted into new `app/api/_compliance_handler.py`:

- `raise_if_banned_client_ua(request, db, key_record)` — 451 path.
- `raise_for_no_substitute_exception(exc, *, request, db, key_record, orig_request_model)` —
  503 conversion. Single catch block (the LocalProvider error is a
  subclass) so callers don't duplicate the try-except.
- `emit_substitution_disclosure_for_route(request, db, route, key_record, orig_request_model)` —
  returns `(headers_to_merge, sse_disclosure, wants_prelude)`. Caller
  unpacks unconditionally.
- `disclosure_headers_for_upstream_error(request, db, route, key_record, orig_request_model, status_code)` —
  returns headers dict for the upstream-error path. Writes the
  follow-up audit row. Swallows audit-write failures (v5.0.1
  guarantee).

The v5.0.6 invariant (`requested_model` field is the caller's
ORIGINAL, captured at the top of the handler before the v3.0.36 body
rewrite) is enforced via signature — every helper takes
`orig_request_model` as a parameter, never reads `body["model"]`
itself.

### Files impacted

- `app/api/_compliance_handler.py` (NEW, 377 LOC)
- `app/api/messages.py` (1095 → 929; -166)
- `app/api/completions.py` (961 → 795; -166, now back under design.md's 800 trigger)
- `tests/unit/test_v509_compliance_handler_extraction.py` (NEW, 6 tests):
  helper exports + static grep that messages.py + completions.py
  no longer inline the patterns + invariant that `_orig_request_model`
  is passed at every call site (the v5.0.6 contract).
- `tests/unit/test_v5_messages_ua_block.py` — repointed the two
  source-grep tests to look in the helper instead of the request
  handlers.
- `tests/unit/test_v5_disclosure_headers.py` — same repoint.
- `tests/unit/test_v5_compliance_endpoints.py` — updated
  test_me_compliance_returns_effective_blocklist for v5.0.8's
  Request-based endpoint signature (the v5.0.8 dual-auth ship had
  changed the function signature; the original test bypassed
  request-based auth).
- `architecture.md` — module map gained `_compliance_handler.py`.

### Risks

- The 503 / no-local-503 paths now share a single catch block (the
  subclass relationship handles the dispatch internally). A future
  contributor splitting them back out would need to also remove the
  isinstance check in `raise_for_no_substitute_exception`. The static
  test `test_emit_event_uses_orig_request_model_for_compliance_audits`
  (from v5.0.6) catches the audit-field regression that bit pre-v5.0.6.

### Decision NOT to continue with messages.py litellm/CoT dispatch tail extraction

The refactor-log's "next target #1" from the v3.10.9 entry was to
extract the litellm tail of messages.py. Re-evaluating after #1
landed: the file is now at 929 LOC (vs the 800 trigger). What
remains is mostly setup + delegations to ALREADY-EXTRACTED modules
(`_messages_dispatch`, `_messages_streaming`, `_grok_web_dispatch`,
`_codex_oauth_dispatch`, the new `_compliance_handler`). The litellm
dispatch tail has heavy local-variable surface (extra, hint, body,
x_cot_*, etc.) — extracting would create a fuzzy-bordered module
purely to gain ~250 LOC, against design.md's "could be split but
isn't painful — leave it." The high-frequency-touched code (the
compliance orchestration) is now centralized; the litellm tail
hasn't been edited in months. Stop here; queue when there's a
concrete trigger.

### Remaining issues / next refactor targets

1. **app/cluster/sync.py** (1024 LOC) — over 800 trigger. v5.0.5 added
   `_section_commit` helper but didn't extract per-table sub-applies
   into named functions. Each table's merge loop is 30-80 LOC; extracting
   each one into a static method on a `_SyncContext` (which holds db,
   peer_costs, settings_to_apply, etc.) would bring sync.py to ~500 LOC
   and make adding new tables to the cluster sync push obvious.
   Deferred this pass: cluster/sync.py was the v5.0.5 incident area;
   touching it in the same week as the incident invites a regression
   the soak might not catch.
2. **app/routing/router.py** (837 LOC) — marginal over 800. The v5.0.0
   compliance pre-filter added ~60 LOC. Lower urgency.
3. **app/providers/grok_web.py** (825 LOC) — refactor-log's v3.10.9
   #3 target, "split along manual/bridge axis." Still pending.

### Tests

2625 unit tests pass (6 new in `test_v509`; 5 repointed/updated;
1 v5.0.8 endpoint test signature-fixed). Behavior preserved —
verified by the full suite. No live deploy needed for this pass yet
since the helper module is import-shape-equivalent to the inlined
code; will ship as part of the next code change that lands on the
fleet.

---

## v5.0.10 — Extract api_keys + providers merges from sync.py

**Date:** 2026-06-04
**Shipped:** v5.0.10

### Motivation

sync.py was 1024 LOC (>800-LOC design.md trigger). The two largest
inline blocks — the api_keys merge (189 LOC) and the providers merge
(262 LOC) — totaled ~451 LOC inside `apply_sync()`. The other
per-table sub-applies (blocked_ips, ai_reviews, caller_memory, etc.)
already live in `sync_handlers.py` under an `_apply_<table>` naming
pattern. The api_keys + providers blocks were extracted in this pass
to bring sync.py under the 800-LOC trigger and unify the codebase
around the existing handler pattern.

### Extraction

Added to `app/cluster/sync_handlers.py`:

- `_parse_iso_naive_utc(v)` — strips tzinfo for naive comparisons
  against SQLite-loaded datetimes (the helper that was inline at
  `apply_sync._parse_iso`).
- `_parse_iso_keep_naive(v)` — accepts both `datetime` and ISO
  strings; used for the api_keys `deleted_at` column.
- `_apply_api_keys(db, rows) -> dict[str, float]` — full api_keys
  merge: tombstone-aware LWW (v4.4.20), full-field UPDATE coverage
  (v4.4.18), full-field INSERT coverage (v4.4.25), BUG-080 .limit(1)
  guards, v5.0.0 compliance-cache invalidation on per-key policy
  change. Returns the per-key `total_cost_usd` map so apply_sync can
  stash it in `_peer_key_costs[source_node]`.
- `_apply_providers(db, rows) -> None` — full providers merge:
  tombstone semantics, dual-lookup (by id then name), v3.5.9 BUG-012
  CB-state cleanup on disable, v3.2.7 legacy updated_at fallback,
  manual-override + codex/anthropic capture fields preserved,
  `register_provider()` invoked on every applied row. BUG-080
  .limit(1) guards on both lookups.

In `apply_sync()` (sync.py), the inline blocks collapsed to:

```python
peer_costs = await _apply_api_keys(db, payload.get("api_keys", []))
_peer_key_costs[source_node] = peer_costs
await _section_commit("api_keys")

await _apply_providers(db, payload.get("providers", []))
await _section_commit("providers")
```

The inner `_parse_iso` function definition (15 LOC) and the
`from app.monitoring.status import register_provider` import (both
inside `apply_sync()`) are now in the handlers, not in sync.py. The
remaining `_parse_iso` reference at model_capabilities (line 373 pre-
refactor) was repointed to the new module-level `_parse_iso_naive_utc`
imported from sync_handlers.

### Result

- `app/cluster/sync.py`: 1024 → 573 LOC (under the 800 trigger,
  matching the post-#1 prediction in the prior refactor-log entry).
- `app/cluster/sync_handlers.py`: 685 → 1002 LOC. Still under 1500
  (the next trigger band per design.md); the per-table handler
  pattern naturally distributes by table count.
- `apply_sync()` body now reads as a clean orchestrator: each
  `_apply_*` call paired with its `_section_commit` boundary. New
  developers adding a table to the sync push have a clear pattern.

### Tests

2631 unit tests pass (6 new in `tests/unit/test_v510_sync_extraction.py`;
4 static-pin tests repointed to `sync_handlers.py`; 2 BUG-080 .limit(1)
guards added to handlers to satisfy `test_v4424_cluster_sync_robustness`).
Behavior preserved — all the existing tombstone, LWW, field-coverage,
and compliance-cache invalidation tests continue to pass.

### Risks

- The api_keys merge now releases the v5.0.5 section commit boundary
  via a return-then-assign hop (helper returns peer_costs; caller
  assigns to `_peer_key_costs[source_node]` then commits). If a
  future contributor inlines the helper back without preserving the
  commit boundary, the v5.0.5 slow-degradation bug returns. The
  static test `test_section_commit_boundaries_preserved` catches the
  removal of the per-section commits.
- The two extracted helpers grew from a place that touched module-
  level state (`_peer_key_costs` for api_keys; `register_provider`
  for providers). The api_keys peer_costs is now passed back via
  return value (no module state coupling); providers'
  `register_provider` was already a side-effect call into another
  module (no change). Net: the extraction strictly reduces coupling.

### Remaining issues / next refactor targets

1. **app/routing/router.py** (837 LOC) — marginal over 800. The
   v5.0.0 compliance pre-filter added ~60 LOC. Lower urgency.
2. **app/providers/grok_web.py** (825 LOC) — refactor-log's v3.10.9
   #3 target, "split along manual/bridge axis." Still pending.
3. **app/providers/_avaya_scraper.py** (1080 LOC) — the largest
   remaining file. Logical split candidates: capture loop / parse /
   persist. Lower priority than router.py — Avaya scraper is
   relatively cold (no active feature work).

---

## v5.0.14 — `/metrics` route disambiguation

**Date:** 2026-06-04
**Shipped:** v5.0.14

### Motivation

`@app.get("/metrics")` (Prometheus scrape endpoint) and the React Router `/metrics` route (`MetricsPage`) lived at the same URL. The FastAPI route always intercepted, so any browser typing the URL got raw text/plain Prometheus output instead of the UI. The conflict was latent until 2026-06-04 when the operator hit the URL directly via a fresh tab (in-app sidebar navigation never triggers a full HTTP request).

### Approach

Accept-header sniffing on the route handler — the smallest semantic change that preserves both audiences:

- Browsers always include `text/html` in `Accept` → serve `frontend/dist/index.html` so React Router renders `MetricsPage`.
- Prometheus scrapers send `Accept: */*` or `text/plain;version=0.0.4` → existing Prometheus response.
- No-Accept requests (bare wget/httpie) → Prometheus (don't break naive scrapers).

### Result

Zero behavior change for external Prometheus consumers (Grafana Cloud Agent, vmagent, prometheus itself all omit `text/html` from `Accept`). The browser path that was previously broken is now correct.

### Risks

- Any future caller that sends `text/html` in Accept thinking it's a hint about content negotiation would get the SPA shell instead of Prometheus data. This is the standard `Accept` semantic, so it's acceptable — but if it bites, the fallback path is documented in the handler's docstring.

### Tests

`tests/unit/test_v5014_metrics_route_disambiguation.py` (4 new):
- Source pin: handler reads `Accept` and branches on `text/html`.
- Behavioral: browser Accept → `FileResponse`; Prometheus Accept → text/plain; no-Accept defaults to Prometheus.

---

## v5.0.15 — Rotation reads both utilization buckets

**Date:** 2026-06-04
**Shipped:** v5.0.15

### Motivation

`app/routing/external_rotation.evaluate_rules_for_provider` only checked `seven_day_utilization`. The Anthropic billing snapshot captures a SEPARATE `five_hour_utilization` (session window, resets every 5h). Hit live on 2026-06-04: `Devin-Anthropic-Max-VG` showed `five_hour=100% / seven_day=13%` and stayed in rotation because the weekly bucket looked healthy. Operator's workaround was a manual `auto_skip_until` set; v5.0.15 is the proper fix.

### Approach

Treat each bucket according to its actual semantics:

- Session bucket (`five_hour_utilization`): hard 100% cap, no hysteresis. It's an upstream lockout, not a tunable policy.
- Weekly bucket (`seven_day_utilization`): existing soft threshold + hysteresis preserved.
- Skip if EITHER exhausts. `auto_skip_until` = LATER of the two active resets so we don't release prematurely while one bucket is still capped.
- Clear only when BOTH are confirmed healthy. A missing value (None) treated as healthy so a transient scraper window-loss doesn't strand a provider.

### Result

Same data path (the snapshots already carry both buckets), more accurate rotation. Backward-compatible with cursor-oauth and older Anthropic snapshots that don't populate `five_hour_*` — those continue to drive on the weekly bucket alone.

Live dry-run against the VG snapshot post-deploy returned `skip_set` until `2026-06-04T23:40:00.990777` (Anthropic's exact reset time), matching the manual workaround that was in place.

### Risks

- A provider where both buckets briefly exhaust could see `auto_skip_until` set to the LATER reset (weekly) and stay skipped for the full weekly window — correct behavior, but worth flagging if it surprises an operator.
- The `evaluate_rules_for_provider` return dict gained a new key (`five_hour_utilization`). Admin endpoint consumers that strict-validate the response shape would need to be aware. Internal-only; no external API contract.

### Tests

`tests/unit/test_v5015_external_rotation_five_hour.py` (8 new):
- The VG incident: session=100% / weekly=13% → skip until session reset.
- Both buckets exhausted → skip until LATER reset.
- Clears only when BOTH buckets recover.
- Weekly-only / session-only / both-None backward compat.
- No_change when both healthy.

Updated `tests/unit/test_v371_external_rotation.py` `_snapshot()` helper to default `five_hour_*` to None so weekly-only tests preserve their pre-v5.0.15 semantics (1-line change). 2650 unit tests pass.

### Remaining issues / next refactor targets

Per-model breakdowns from `seven_day_sonnet_utilization` / `seven_day_opus_utilization` could drive per-model skip decisions (e.g., skip a provider only for Sonnet when only the Sonnet bucket is at 100%). Same data shape, no concrete operator trigger yet — deferred.

---

## v5.0.16 — Nginx routing hardening (ops-only, no version bump)

**Date:** 2026-06-04
**Shipped:** nginx config changes on tmrwww01 / tmrwww02 / c1conv. No proxy code change, no docker image push, no version tag.

### Motivation

The variable+rewrite fix shipped earlier 2026-06-04 (alongside the v5.0.12 deploy) eliminated the *catastrophic* version of the nginx stale-upstream-cache bug — where `proxy_pass http://llm-proxy2:3000/` was being resolved once at config-load and silently routing `/llm-proxy2/` URL to whatever container happened to hold the cached IP. But it left two residual issues:

1. **Stale-cache window after recreate.** The parent server block's `resolver 127.0.0.11 valid=30s;` bounded re-resolution at 30s. After a container recreate that swapped IPs (saw this twice today during v5.0.12 and v5.0.15 deploys), nginx could route to the wrong backend for up to 30s. In practice the window seemed longer — operator hit the bad state ~11 minutes post-deploy on the second occurrence, suggesting docker's embedded DNS at 127.0.0.11 was also caching beyond the nginx-stated TTL.

2. **One bare-hostname holdout** at the `/grok-bridge-auth-check` auth_request subrequest block: `proxy_pass http://llm-proxy2:3000/api/auth/me;`. Not part of the variable-form sweep because it's an `internal` auth_request, not a user-facing route. Same stale-cache risk; same silent failure mode (auth subrequest hits smoke's session table → operator bounced to login with no clear error).

### Approach

1. **Tighten resolver TTL** from `valid=30s` to `valid=5s ipv6=off` on the llm-proxy2 server block (tmrwww01) and host-wide (tmrwww02, c1conv — they have one resolver, so scope is broader but the cost — more frequent ~1ms-each docker DNS lookups — is negligible). `ipv6=off` because docker's embedded DNS lacks IPv6 anyway; suppressing the IPv6 query halves the resolution cost.
2. **Convert the bare-hostname holdout** to `proxy_pass $llm_proxy2_upstream/api/auth/me;`. Re-uses the existing `set $llm_proxy2_upstream …` directive declared earlier in the same server block.

### Result

Worst-case stale-IP window after a recreate: ≤5s (down from ≤30s observed, ≤several minutes worst-case earlier today). Zero bare-hostname holdouts remain in the llm-proxy2 routing path. Stress-tested via `--force-recreate llm-proxy2-smoke`: routing held correctly throughout the 8s sample window.

### Risks

- Tighter resolver TTL means ~6x more DNS queries against docker's embedded resolver. Cost is sub-millisecond per query (in-memory lookup inside the docker daemon); no measurable performance impact.
- The deeper question — *why* did the routing stay stale for several minutes once today rather than the expected ~30s — wasn't fully diagnosed. Hypothesis: docker's embedded DNS at 127.0.0.11 returns answers with longer-than-stated TTLs under some conditions and nginx's resolver respects the upstream TTL when it's shorter than `valid=` (though the docs imply `valid=` overrides). Worth a separate diagnostic if the issue recurs even with `valid=5s`.
- Configs are host-local (not in git, not in backup tarballs). Backups left at `nginx.conf.bak-pre-v5016-<UTC-timestamp>` on each host for rollback.

### Tests

No tests — these are nginx configs, not proxy code. Verification was live: post-restart routing checks on all three hosts + a `--force-recreate llm-proxy2-smoke` stress test that confirmed routing held across the recreate.

---

# Archived — early refactor passes R1-R5 (merged from `docs/refactor-log.md`, 2026-08-17)

A second `refactor-log.md` lived under `docs/` with the same disjoint-content problem as
the bug logs: these four R1-R5 entries (2026-05-09/10) existed only there and appear
nowhere above. Merged so there is one refactor log.

## 2026-05-10 — Extract `record_outcome` success/error duplication (R5, v3.7.13+)

**What**: `record_outcome` in `app/monitoring/helpers.py` was a single
275-line function with two parallel branches (success / error) that
duplicated:
- The branch-agnostic `meta` dict construction (model, served_model,
  provider_name, api_key_prefix, api_key_id, cost_class)
- The client-IP capture try/except (v3.6.2 client_ip + v3.6.3
  client_ip_inside split)
- The optional caller-hint fields (requested_model, had_lmrh_hint,
  lmrh_hint_raw, lmrh_warnings, probe)
- The `log_event` call with `event_type="keepalive_probe" if is_probe
  else "llm_request"`

Extracted three private helpers:
- `_attach_client_ip(meta)` — mutating helper for the v3.6.2/v3.6.3
  client_ip + client_ip_inside fields (single source of truth for the
  IP capture contract)
- `_build_event_meta_base(...)` — builds the shared base dict + calls
  `_attach_client_ip` + appends the optional caller-hint fields
- `_emit_outcome_event(...)` — wraps the `log_event` call so the
  v3.3.4 keepalive/llm event-type split lives in one place

Each branch now: calls `_build_event_meta_base`, adds branch-specific
fields (success: in_tok/out_tok/cost_usd/latency_ms/quota_usd/cache_*;
error: error/error_class), then calls `_emit_outcome_event`.

**Why**: every change to the shared meta fields had to land twice.
Confirmed: v3.6.2's `client_ip` add, v3.6.3's `client_ip_inside`
split, and v3.6.2's `api_key_id` all required dual edits — the
operator hit this themselves while reviewing the v3.6.x diffs.
v3.7.x added 13 ships in one day; if any of them had needed a new
shared field, that's 13 chances to forget the second edit.

**Files**:
- `app/monitoring/helpers.py` — three new private helpers
  (`_attach_client_ip`, `_build_event_meta_base`, `_emit_outcome_event`);
  both branches of `record_outcome` rewritten to use them
- `tests/unit/test_v362_request_context.py` — regression-check
  updated to verify the new helper structure (looks for
  `_attach_client_ip` + `_build_event_meta_base` call from both
  branches)
- `tests/unit/test_v363_lan_egress_rewrite.py` — same update

**Outcome**:
- `record_outcome` body: 275 → **193 lines** (−82, −30%)
- Helpers add ~110 lines (including verbose docstrings explaining the
  v3.6.2/v3.6.3/v3.3.4 history once instead of twice)
- File total: 477 → 500 lines (+23)
- **Maintenance surface**: 2 places to update shared meta fields → 1
- Tests: **1338 passing** (no behavior change — confirmed by zero
  test count delta and unchanged test file count)

**Audit notes** (other candidates surveyed, not picked):
- `app/api/providers.py` (1028L, was 952 at R4) — still below 1200L
  threshold per the deferred list; largest function is `create_provider`
  at 143L which is at the right granularity (provider creation is
  intrinsically multi-field)
- `app/api/lmrh_v2.py` (647L) — still below 800L threshold per R4
- `app/models/db.py` (620L, ~13 ORM classes) — splitting across files
  would conflict with the architecture-rule "models/ is the only
  module that defines SQLAlchemy table classes" (single Base.metadata
  invariant); over-fragmentation. Skip.
- Three new v3.7.x admin endpoint files (anthropic_billing, ai_rate_limiter,
  blocked_ips) — each ~100-180L with distinct serializers and
  endpoint shapes. Already at the right granularity. No duplication.
- `create_provider` (143L) + `update_provider` (110L) — large but
  cohesive; each new provider field naturally adds one line in each.
  Splitting wouldn't help. Skip.

**Next** (deferred list, in priority order):
1. Tool emulation / streaming extraction (R1/R2 deferred — only on
   concrete bug forcing edits to both messages.py + completions.py)
2. `app/api/lmrh_v2.py` (647L) split into endpoints + render modules —
   pre-emptive, defer until 800L
3. `app/api/providers.py` per-CRUD splits — DEFERRED under
   intuitiveness rule. Reconsider if file crosses 1200L
4. Alembic migration framework — defer until ALTER count crosses 50
   (currently ~40, +3 from v3.7.x — billing, ai-review, ip-block)

---

## 2026-05-09 (evening, third pass) — Extract claude-oauth request setup (R4, v3.5.5+)

**What**: `_complete_claude_oauth` and `_stream_claude_oauth` in
`app/api/_messages_streaming.py` each opened with the same 4-line
URL + body-mutation block, plus they each declared the
`httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)`
verbatim. Extracted to:

- `_CLAUDE_OAUTH_TIMEOUT` module constant — single source of truth for
  the split-phase timeout config (the v3.0.60 rationale lives in the
  constant's docstring now).
- `_prepare_claude_oauth_request(body, *, stream)` helper returning
  `(url, prepared_body)`. Handles URL construction, `max_tokens`
  default, claude-code system injection, and the stream flag in one
  place.

**Why**: the duplication is small (~10 LOC literal + the conceptual
"this is how a claude-oauth request is shaped") but every future
change to Anthropic's URL conventions, beta header layout, or
timeout config now lands in one helper instead of two function
bodies. Also extracts a future-friendly seam if a third
claude-oauth dispatch path ever shows up (e.g. tool-use-only,
batch).

The 401-refresh-and-retry loop was NOT extracted — too different
between the dict-returning complete and bytes-yielding stream
paths to extract cleanly without a generator-based driver, and the
payoff would be marginal. Deferred until a 3rd OAuth provider
makes the pattern worth abstracting.

**Files**:
- `app/api/_messages_streaming.py` — added `_CLAUDE_OAUTH_TIMEOUT`
  constant + `_prepare_claude_oauth_request` helper; both call
  sites updated; verbose pre-fix comments folded into the helper

**Outcome**:
- `_messages_streaming.py`: 701 → 727 lines (+26; helper docstring
  is verbose on purpose, explains the v3.0.60 split-timeout history
  + the URL convention rationale once instead of twice)
- **Maintenance surface**: 2 places to update timeout / URL / body
  defaults → 1 place each
- Tests: **1040 passing** (no regression)

**Audit notes** (other candidates surveyed, not picked):
- `app/api/providers.py` (952L) — still deferred under the
  intuitiveness rule. 21 functions in one logically-named file
  is the right organization; splitting per-CRUD would worsen
  lookup speed.
- `sdk/python/lmrh_client.py` (666L, up from 448L pre-v3.5.2
  subscribe additions) — still cohesive (one `LmrhClient` class).
  Splitting would create import noise.
- `app/api/_grok_web_dispatch.py` (515L) — openai-shape +
  anthropic-shape variants already share the dispatch helpers
  via parameter passing; no clear seam for further extraction.

**Next** (deferred list, in priority order):
1. Tool emulation extraction (R1/R2 deferred — only if concrete
   bug forces editing both messages.py + completions.py).
2. Streaming + hedging extraction (same condition).
3. `app/api/lmrh_v2.py` (647L) split into endpoints + render
   modules — pre-emptive, defer until 800L.
4. `app/api/providers.py` per-CRUD splits — DEFERRED under
   intuitiveness rule. Reconsider if file crosses 1200L.

---

## 2026-05-09 (PM, second pass) — Extract grok_web manual-mode HTTP setup (R3, v3.5.0+)

**What**: The 3 dispatch functions in `app/providers/grok_web.py`
(`complete_grok_web`, `stream_grok_web`, `stream_grok_web_anthropic`)
each opened with the same 6-line setup pattern:

```python
conv_id = _pick_conversation_id(provider_extra_config)
mode_id = _model_to_mode_id(model)
url = f"{GROK_BASE_URL}/rest/app-chat/conversations/{conv_id}/responses"
headers = _build_headers(provider_extra_config, conv_id)
body = _build_body(prompt, mode_id)
```

Extracted to a single helper `_build_manual_request(extra_config, prompt, model)`
returning `(conv_id, mode_id, url, headers, body)`. Each dispatch
function now opens with the helper call + format-specific httpx
invocation; the URL pattern, header convention, and body shape
live in one place.

**Why**: Three places to update if any of the conventions ever
change (e.g. grok.com adds a header, the URL pattern changes,
the modeId mapping picks up a new tier). The 6-line setup wasn't
"big" duplication but it was conceptually load-bearing — a future
fix to the URL or header construction would have to land 3 times.
The audit also identified `app/api/providers.py` (939L, 21
functions) as the largest file but explicitly DEFERRED that split
under the operator's "avoid over-fragmentation" rule — 21 functions
in one logically-named file is intuitive; splitting into per-CRUD
files would worsen lookup speed.

`_messages_streaming.py` (701L) was also surveyed — it's bigger
than `_completions_streaming.py` because it carries the
claude-oauth-specific helpers (`_complete_claude_oauth`,
`_stream_claude_oauth`, `_inject_claude_code_system`,
`_refresh_oauth_token`), not because of duplication. Correct
asymmetry, no extraction.

**Files**:
- `app/providers/grok_web.py` — added `_build_manual_request`
  helper (~50 lines including the docstring); collapsed 3 inline
  6-line setup blocks to 3-line helper calls

**Outcome**:
- `grok_web.py`: 812 → 845 lines (+33; helper docstring is verbose
  on purpose — explains the 3-call-site rationale)
- **Maintenance surface**: 3 places to update URL/header/body
  setup → 1 place
- Tests: **1035 passing** (no regression)

**Next**: Tool emulation (~35 lines, 60% duplicated) and
streaming/hedging orchestration (~59 lines, 70% duplicated)
remain on the deferred list from R1/R2 — still recommended ONLY
when a concrete bug forces editing both messages.py and
completions.py. R4 candidates from this pass:

- `_messages_streaming.py` line 376–600 region: the
  claude-oauth dispatch+stream pair has internal duplication of
  request-body construction. A helper similar to
  `_build_manual_request` could absorb header + body shaping.
  ~30 lines of duplication, lower payoff than R3.
- `app/api/providers.py` per-CRUD endpoint splits — DEFERRED per
  intuitiveness rule. Reconsider if file crosses 1200L or if a
  single function bloats past ~150 lines.
- `app/api/lmrh_v2.py` (647L) — split into endpoints + render
  modules pre-emptively. Currently below the 800L threshold;
  defer until it crosses.

---

## 2026-05-09 — Extract cache + CoT orchestration to `_request_pipeline` (R1+R2, v3.5.0+)

**What**: The cache-decision-and-serve block (35 lines, 100% duplicated)
and the CoT-E engagement block (42 lines, 80% duplicated) lived in both
`app/api/messages.py` and `app/api/completions.py`. Extracted to two
helpers in `app/api/_request_pipeline.py`:

- `maybe_serve_from_cache(...) → (CacheDecision, Optional[Response])`
- `maybe_engage_cot(...) → Optional[StreamingResponse]`

Both helpers take the wire-format-specific bits (SSE / JSON builders,
stream functions) as callable parameters, so the Anthropic and OpenAI
shapes pass their own builders without duplicating the orchestration.

**Why**: Pre-R1 a bug fix in cache decision logic had to land in two
places, and the same was true for CoT critique-provider pickup. The
2026-05-09 audit (Explore agent estimate) found ~250 lines of true
duplication between the two endpoints, of which cache + CoT were the
top 2 highest-value extractable patterns. Tool emulation + hedging
were also identified but deferred to avoid over-fragmentation in a
single pass.

A near-bug surfaced during R1: the first cut of `maybe_serve_from_cache`
returned only the response (or None), losing the `cache_decision`
local that downstream `maybe_store()` calls relied on. The
`try: maybe_store(...) except Exception: pass` blocks silently swallowed
the resulting NameError so tests passed but cache write-back was quietly
skipped. Caught during line-count review; helper now returns the
decision tuple so callers can pass it onward.

**Files**:
- `app/api/_request_pipeline.py` — added two helpers (~190 lines including docstrings)
- `app/api/messages.py` — replaced cache + CoT inline blocks with helper calls; cleaned 3 now-unused imports
- `app/api/completions.py` — same; cleaned 3 now-unused imports

**Outcome**:
- `messages.py`: 813 → 783 lines (−30)
- `completions.py`: 630 → 601 lines (−29)
- `_request_pipeline.py`: 312 → 501 lines (+189)
- Net file LOC: +130 (helper docstrings explain why, which is the point)
- **Maintenance surface**: TWO places to update cache or CoT logic → ONE
- Tests: **1035 passing** (no regression; same count as pre-refactor)

**Next**: Tool emulation (~35 lines, 60% duplicated) and streaming/hedging
orchestration (~59 lines, 70% duplicated) are the next highest-value
extraction targets per the 2026-05-09 audit. Recommended ONLY if a
concrete bug shows up that requires editing both endpoints — otherwise
the current state is the right balance between sharing and clarity.
The `_request_pipeline.py` module should not exceed ~700 lines or it
itself becomes the over-fragmentation problem; if more orchestration
needs sharing, consider splitting into
`_request_pipeline/cache.py` + `_request_pipeline/cot.py` etc.

---

(no earlier entries — this is the first formal refactor pass after the
2026-05-09 v3.5.0 model-identity work)
