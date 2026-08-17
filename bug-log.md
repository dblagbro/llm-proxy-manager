# Bug Log — llm-proxy-v2

Persistent log of defects, regressions, and quality gaps discovered during
QA/regression sweeps. Add new findings at the top with the most recent
sweep date as the section header.

Severity ladder: **critical** > **high** > **medium** > **low** > **enhancement**

Status flow: **open** → **in-progress** → **fixed** → **verified-fixed** → **wont-fix**

---

## 2026-08-10 — `_next_route` infinite loop wedges nodes (v5.22.6)

**Severity: critical · Status: fixed (deploy pending operator approval)**

`app/routing/fallback.py::_next_route` spun forever whenever `select_provider`
returned an already-tried provider: it excluded only ONE seed
(`next(iter(extended_excluded))`) and "progressed" by re-adding an id already in
the set — a no-op — so the seed never changed. Every pass ran `select_provider`
→ `_load_profile` × providers → ~2 queries each.

**Impact.** A single `/v1/messages` request that hit a provider error pegged the
event loop and drained the DB pool to 50/50 on an idle node; `/health` 500'd,
the node never recovered, and SQLite writes stopped dead (WAL mtime frozen 9h on
tmrwww01). Recurred across v5.22.3/.4/.5 because those fixed connection holding
and aiosqlite thread leaks — different bugs.

**Evidence.** Independent py-spy captures on tmrwww01 and tmrwww02, same stack:
`_load_profile ← select_provider ← _next_route ← try_ranked_non_streaming ←
messages.py:1030`. MainThread ~44% of a core, ~50 aiosqlite threads ~2.9% each,
traffic ~1 req/2min. Asymmetry (tmrwww01 64 pool errors/healthy vs tmrwww02
24,243/wedged on identical code) = whether a fallback-triggering request arrived.

**Fix.** Pass the cumulative `exclude_provider_ids` set that `select_provider`
has supported since v5.7.13; defensive guard raises instead of looping if an
excluded provider ever comes back. Pin:
`tests/unit/test_v5226_next_route_terminates.py` (verified failing pre-fix).

**Retires:** the "P2 single-event-loop CPU ceiling under abusive concurrency"
entry — the node was spinning, not saturated.

---

## 2026-06-12 — security-team mandated pre-compliance data purge (v5.4.3)

Not a regression finding — operator-filed backlog item from 2026-06-06 ("when clear up old metrics in the clusters — ask me more about this when done with all the ongoing work"). Scoping interview 2026-06-12 revealed the actual ask: delete data accumulated before the v5.2.0 vendor-neutrality stack shipped, per security team mandate.

**Decision (operator, 2026-06-12):**
- Cutoff: **2026-06-06 00:00 UTC** (v5.2.0 ship date)
- Tables: `activity_log`, `provider_metrics`, `provider_ai_review`
- Scope: 3 `/llm-proxy2/` instances ONLY (compliance-locked URL). `/llm-proxy/` clone left intact (outside the compliance envelope, like BUG-071).

**Implementation:** v5.4.3 ships `POST /api/admin/compliance-epoch-purge` with PURGABLE/FORBIDDEN allow-lists, `dry_run` default, audit row written BEFORE deletes. 9/9 pin tests.

**Applied 2026-06-12 21:20 UTC:**

| Instance | activity_log | provider_metrics | provider_ai_review | Total | Audit row |
|---|---|---|---|---|---|
| tmrwww01 llm-proxy2 | 582 | 344 | 6,204 | 7,130 | `ppc_0019ebe94db2272bdaff8d1d5` |
| tmrwww02 llm-proxy2 | 573 | 338 | 5,918 | 6,829 | `ppc_0019ebe94e906a5f74fc2816d` |
| c1conv llm-proxy2 | 837 | 414 | 5,880 | 7,131 | `ppc_0019ebe94fea4444c08a9f9c6` |
| **Total** | **1,992** | **1,096** | **18,002** | **21,090** | — |

Post-purge dry-run survey on all 3 instances returned `matched=0` (zero remaining pre-cutoff rows). compliance_events table NOT touched (verified `oldest = 2026-06-04` preserved). The audit chain itself is untouched and continues signing daily.

Smoke instance had zero pre-cutoff rows (sandbox). Clone instances (tmrwww01/02 `/llm-proxy/`) left intact per operator decision.

---

## 2026-06-12 — post-refactor regression sweep (v5.1.0 → v5.3.9)

Deep sweep covering **v5.1.0 → v5.3.9** (~36 release-level diffs over 7
calendar days). Spans the v5.2 vendor-neutrality stack (V1 emergency
stop + V2 fine-grained policy + V3 docs/report), v5.3.0 policy editor
UI, v5.3.1 no-op substitution skip, v5.3.2 compliance taxonomy
endpoint, v5.3.3 BoolSystemSetting factory refactor, v5.3.4 openai-python
retry tap, v5.3.5 cursor billing parity, v5.3.6 cursor-bridge list-content
emulation, v5.3.7 Gemini thinking-budget clamp, v5.3.8 logical alias
routing, and v5.3.9 CB lifecycle hardening. Environment: 6 prod
endpoints (3 `llm-proxy2` TMR nodes + 2 `llm-proxy` clone nodes + smoke
+ c1conv GCP); pre-sweep unit baseline **2670** (2026-06-05) →
post-sweep **2910 passing + 2 skipped** (clean, ~42s). Methods: full
pytest suite, fleet-wide `/health` parity probe, deep DB probe on
tmrwww01, audit-chain freshness check, activity-log severity sweep,
supervisor / cluster-sync / retry-tap liveness audit.

Fleet parity confirmed: 6/6 endpoints on **v5.3.9** at sweep start.
No version skew; the daily fleet-health routine (scheduled
2026-06-08) has its first datapoint.

Findings BUG-069+ (BUG-001..068 already used).

### High

#### BUG-069 — Background-worker liveness not surfaced in `system_settings` → silent failures invisible from snapshot
- **Component:** `app/monitoring/*` (`ai_provider_supervisor.py`, `keepalive.py`, `billing/*`, `cluster/sync.py`, `observability/openai_retry_tap.py`).
- **Severity:** high (operability) — every background loop the proxy depends on for self-healing is unobservable from outside its own log line. A hung worker stays hung until someone notices a downstream symptom hours/days later.
- **Repro:** `SELECT key, value FROM system_settings WHERE key LIKE '%_worker_%' OR key LIKE 'last_%' OR key LIKE 'cluster_sync_%' OR key LIKE 'ai_provider_supervisor_last_%'` returns **zero rows** on tmrwww01 — despite the keepalive worker actively running (5 `keepalive_probe` events in the last hour) and the AI supervisor having `ai_provider_supervisor_enabled = True`.
- **Evidence:** Deep probe `/tmp/qa_deep_probe.py` section A returned empty; `ai_provider_supervisor_last_run` and `ai_provider_supervisor_last_review_count` both NULL; `cluster_sync_last_run` / `cluster_sync_last_status` / `cluster_sync_consecutive_failures` / `cluster_last_push_at` all NULL.
- **Cause:** Workers print to container stderr but never write a `last_run` / `last_status` / `consecutive_failures` row to `system_settings`. There's no equivalent of the `BoolSystemSetting` factory (v5.3.3) for liveness counters.
- **Fix shipped (v5.4.0):** `app/monitoring/worker_heartbeat.py::WorkerHeartbeat` factory writes `worker.<name>.{last_run, last_status, last_note}` to `system_settings` on every tick. 4 representative workers wired (`keepalive`, `ai_provider_supervisor`, `anthropic_billing`, `cluster_sync_push`). `/health` envelope gains `workers: [...]` block with `stale=true` flag when `age_sec > 3 * expected_interval_sec`. Remaining 11 workers carried as v5.4.x follow-ups.
- **Pin:** `test_v540_worker_heartbeat.py` (13/13) — factory exports, key shape, idempotent register, 4 wired-worker source-greps, 2 health-envelope contracts, 4 admin-endpoint cases.
- **Status:** **fixed** (v5.4.0 shipped 2026-06-12).

#### BUG-070 — AI provider supervisor enabled fleet-wide but zero supervisor activity in 7d
- **Component:** `app/monitoring/ai_provider_supervisor.py`.
- **Severity:** high (the supervisor is the substantive self-healing layer — Tier A flipped `auto_apply` to True on 2026-06-11; without it actually running, all the v5.3.9 CB hardening fights its battles alone).
- **Repro:** `SELECT event_type, COUNT(*) FROM activity_log WHERE event_type LIKE '%supervisor%' AND created_at >= datetime('now','-7 days') GROUP BY 1` → **zero rows** on tmrwww01. `ai_provider_supervisor_enabled = True`, `ai_provider_supervisor_auto_apply = False` (Tier A flipped this to True but the value here still shows False — possibly a different running node, or the change didn't persist; needs verification).
- **Evidence:** Deep probe section J found `info` is the only severity in the last 24h (513 events), all `llm_request` + `keepalive_probe`. No `provider_review`, no `provider_disable_proposed`, no `supervisor_*` events of any kind.
- **Cause hypotheses:** (a) supervisor crashes silently on first tick, (b) supervisor sleeps forever because `ai_provider_supervisor_interval` is NULL and the default isn't picked up, (c) supervisor runs but its emit-to-activity-log path is broken (would also explain BUG-069 — no heartbeat row), (d) the gate condition (`enabled + interval expired + N qualifying providers`) is never met.
- **Fix shipped (v5.4.0):** `POST /api/admin/ai-supervisor/run-once` admin endpoint synchronously runs `_scan_all_once()` and returns `{ok, counts}` or `{ok: false, error, error_type}` on crash. Bypasses the `enabled` flag intentionally (the whole point is diagnosing a worker that may not be running). Combined with BUG-069's heartbeat row on `ai_provider_supervisor`, the supervisor's state is now observable in two ways: (a) `/health.workers[name=ai_provider_supervisor]` for periodic liveness, (b) `POST /api/admin/ai-supervisor/run-once` for on-demand verification.
- **Pin:** `test_v540_worker_heartbeat.py::test_supervisor_run_once_endpoint_handles_crash` + `::test_supervisor_run_once_endpoint_returns_counts`.
- **Follow-up:** Diagnostic endpoint will surface the root cause on the next operator probe of c1conv / tmrwww01. Carry as **partial-fix**; closed once the diagnostic identifies and addresses the underlying cause.
- **Status:** **fixed (diagnostic)** — v5.4.0 ships the introspection; root-cause fix waits on first probe result.

#### BUG-071 — Compliance policy enforcement not exercised by the dominant production caller (`coordinator-hub`)
- **Component:** v5.0.x + v5.2.x compliance subsystem applied to `api_keys.coordinator-hub`.
- **Severity:** high (audit-trail amounts to "we shipped enforcement but nothing is being enforced for the customer that drives most traffic"; reads as compliance theatre on inspection).
- **Repro (pre-fix):** `SELECT name, blocked_companies, allowed_companies, blocked_models, allowed_models FROM api_keys WHERE name = 'coordinator-hub'` → all four columns NULL on every `/llm-proxy2/` instance. Sibling key `coordinator-code-prod-hub-v2` already carried `blocked_companies = ["anthropic"]`.
- **Cause:** Policy never applied to coordinator-hub key after the v5.2 vendor-neutrality stack shipped — the canary ran policy-free and the window was never closed.
- **Fix applied 2026-06-12:** operator decision — `blocked_companies = ["anthropic"]` on the compliance-locked `/llm-proxy2/` URL + GCP; `/llm-proxy/` clone + TMR hosts (clone) left unrestricted. Applied via direct DB UPDATE (with `last_user_edit_at` bump) + `compliance_policy_changes` audit row on:
    - **tmrwww01 llm-proxy2** — applied directly. audit_id `ppc_0019ebe87c8c18bcc49a7fcbf`.
    - **tmrwww02 llm-proxy2** — replicated via cluster sync (LWW within ~60s).
    - **c1conv llm-proxy2** — applied directly (cluster sync TMR→GCP is broken; c1conv is standalone). audit_id `ppc_0019ebe87ff8d7b1787c6d884`.
    - smoke instance has no `coordinator-hub` key (only `sandbox-coordinator-code-profile`, already blocks anthropic).
    - llm-proxy clone (tmrwww01/www2) — left unrestricted per operator decision.
- **Pin:** carried as v5.4.3 candidate (`test_v543_compliance_dominant_key_has_policy.py`) — assert any key with 0 `compliance_events` in 7d AND >10% llm_request share has at least one non-NULL policy column. v5.4.1 zero-row chain warning remains as the safety net.
- **Status:** **fixed** (2026-06-12; operator decision + 3-instance apply).

#### BUG-072 — v5.3.4 openai-python retry tap shows zero observed retries in 24h
- **Component:** `app/observability/openai_retry_tap.py`.
- **Severity:** medium-high (read as "tap broken" on SQL inspection — turned out to be a Prometheus-only emit; the QA repro queried the wrong source-of-truth).
- **Repro:** `SELECT event_type, COUNT(*) FROM activity_log WHERE event_type LIKE '%retry%' AND created_at >= datetime('now','-1 day')` → **zero rows**.
- **Cause (corrected):** the v5.3.4 ship intentionally wrote ONLY to the Prometheus counter (`llm_proxy_openai_retries_total`), not to `activity_log`. The QA query looked at the wrong source. The tap itself was working — but operators couldn't see it without scraping `/metrics`.
- **Fix shipped (v5.4.1):** the tap now also writes an `openai_client_retry` `activity_log` row alongside the Prometheus increment (best-effort, errors swallowed; Prometheus remains source-of-truth). Added `is_installed()` + `self_test()` introspection helpers + `POST /api/admin/ai-supervisor/retry-tap-self-test` admin endpoint that synthetically emits a retry record and confirms the tap captures it.
- **Pin:** `test_v540_openai_retry_tap_hardening.py` (8/8).
- **Status:** **fixed** (v5.4.1 shipped 2026-06-12).

### Medium

#### BUG-073 — Audit chain dutifully checksums daily zero-row windows → false-positive "audit healthy"
- **Component:** `app/monitoring/compliance_audit_worker.py`.
- **Severity:** medium.
- **Repro:** `SELECT day, row_count, computed_at FROM compliance_audit_chain ORDER BY day DESC LIMIT 5` on tmrwww01 showed 5 consecutive `row_count = 0` days.
- **Cause:** Job is correct in isolation — but pairs poorly with BUG-071 because the absent-policy case turns into "fully signed days of nothing happening."
- **Fix shipped (v5.4.1):** `_emit_zero_row_warning_if_threshold` runs at the end of every audit-worker sweep; if the last 3 chain rows ALL have `row_count = 0`, emits one `warning`-severity `audit_chain_zero_row_streak` `activity_log` row. Idempotent — re-running the worker on the same streak in a 24h window won't multiply the noise. Threshold pinned at 3 (long weekend won't trigger). The warning message names BUG-071 as the likely cause so the operator gets a direct hint.
- **Pin:** `test_v540_audit_chain_zero_row_warning.py` (7/7).
- **Status:** **fixed** (v5.4.1 shipped 2026-06-12).

#### BUG-074 — `cluster_peers` table holds peers but cluster_sync_last_* keys are NULL → can't tell if sync ever runs
- **Component:** `app/cluster/manager.py::_sync_loop`.
- **Severity:** medium (closely related to BUG-069; called out separately because cluster-sync is the canonical example — peers are configured, but the snapshot has no way to tell whether the push loop is alive).
- **Repro:** tmrwww01 has 2 peers configured (`llm-proxy2-www2`, `llm-proxy2-c1conv`). `cluster_sync_last_run` / `cluster_sync_last_status` / `cluster_sync_consecutive_failures` / `cluster_last_push_at` are all NULL.
- **Cause:** Same root cause as BUG-069 — sync push writes nothing to `system_settings`.
- **Fix shipped (v5.4.0):** `_sync_loop` wraps each tick with `WorkerHeartbeat("cluster_sync_push").tick(status, note=f"peers={n} pushed={p} failed={f}")`. Status is `ok` (zero failures), `partial` (some pushed), or `error` (zero pushed). Surfaces in `/health.workers`.
- **Pin:** `test_v540_worker_heartbeat.py::test_cluster_sync_push_loop_calls_worker_heartbeat_tick`.
- **Status:** **fixed** (v5.4.0 shipped 2026-06-12).

### Low / observability

#### BUG-075 — RETRACTED (false positive)
- **Component:** `app/api/cluster.py:33` (the real /health endpoint, NOT `app/api/health.py`).
- **Severity:** retracted.
- **Original claim:** /health JSON does not carry a `dbPool` block.
- **Why it was wrong:** the QA probe only inspected the SQLite snapshot via `qa_deep_probe.py`; I never ran `curl /health | jq .dbPool` against any of the 6 endpoints. The /health endpoint on `app/api/cluster.py:33` (the live one — `app/main.py:669` is a stub) has carried the full block since v3.9.8: `size`, `checked_out`, `overflow`, `in_use`, `max`. When `db_pool_trace=true` (TMR www1+www2+c1conv), it also surfaces `oldest_checkout_age_sec`, `traced_checked_out`, `traced_async_sessions`, `oldest_async_session_age_sec`. The fleet-health routine's `dbPool.checked_out > 20` and `dbPool.oldest_checkout_age_sec > 300` checks already work.
- **Status:** **closed (retracted)**, 2026-06-12 during v5.4.0 verification. Lesson: ground-truth the actual API shape with curl before filing observability bugs from snapshot probes alone.

### Hardening pins shipped in this sweep

| File | Pins | Catches |
|---|---|---|
| `tests/unit/test_v539_cb_hardening.py` | 10 | regression of caller-side classifier, record_outcome wiring (static grep), `_schedule_auto_probe` helper existence, get_state HALF_OPEN transition fires probe (vs not when still holding), hysteresis lock at >=2. |

### Confirmed-healthy areas

- **Fleet version parity:** 6/6 endpoints on v5.3.9 (TMR ×3 + clone ×2 + smoke; c1conv on v5.3.9 too). No version skew at sweep start.
- **Unit suite:** 2910 / 2910 passing + 2 skipped, ~42s. +240 new pins since 2026-06-05 baseline (v5.2 + v5.3.x test files).
- **CB hardening (v5.3.9):** all 10 pins green; classifier correctly drops `Missing corresponding tool call`, `string expected`, `Invalid user message at index` and correctly preserves auth + real upstream 5xx.
- **Hysteresis (`circuit_breaker_success_needed`):** = 2 (≥2 OK; locked vs the v3-era regression-to-1 bug).
- **Compliance taxonomy endpoint (v5.3.2):** wiring intact; frontend reads it cleanly.

---

## 2026-06-05 — post-refactor deep regression sweep (v5.0.17 → v5.0.21)

Deep sweep covering **v5.0.17 → v5.0.21** (5 releases + grok-bridge
refactor + clone-cluster spin-up + DevinGPT key provisioning), spanning
the v5.0.18 cluster-peers feature, v5.0.19 random-prompt + statsig
validation, v5.0.20 SPA-UI-driven chat, v5.0.21 disable_long_context
ContextVar. Environment: 5 prod endpoints (3 `llm-proxy2` nodes + 2
`llm-proxy` clone nodes + smoke); 35 release diffs since the 2026-05-15
v3.10.9 baseline. Pre-sweep: 1973-test baseline (stale by 22+ months
of feature work, actual at sweep start: **2655 passing + 7 failing**);
post-sweep + new pins: **2670 passing**. Methods: full pytest suite,
multi-file code-level audit via Explore agents, live HTTP/curl probing
of every changed endpoint, concurrent-call race testing, container log
inspection across the fleet, schema parity check across 3 clusters.

Findings BUG-049+ (BUG-001..048 already used).

> **Hotfix ships (v5.0.21 + v5.0.18-hotfix)** — BUG-049, BUG-050, BUG-051,
> BUG-052 shipped during this sweep as inline hotfixes; pin tests added
> in `test_v5021_disable_long_context.py`.

### Critical — FIXED during sweep

#### BUG-049 — `disable_long_context` dispatch crashes on test mocks (v5.0.21 RC regression)
- **Component:** `app/api/_messages_dispatch.py:141`, `completions.py:351`, `monitoring/keepalive.py:210`, `providers/scanner.py:424`
- **Severity:** critical (production hot path → `AttributeError` if provider object lacks `extra_config`; 7 unit tests crashing was the signal)
- **Repro:** Run `pytest tests/unit/test_v31015_buglog_fixes.py`. Pre-hotfix: 7/11 fail with `AttributeError: 'types.SimpleNamespace' object has no attribute 'extra_config'`.
- **Evidence:** Test output captured at v5.0.21 RC.
- **Cause:** v5.0.21 added `set_disable_long_context(bool(route.provider.extra_config…))` calls without defensive attribute access; mocks (and any non-ORM provider object) crash the dispatch site.
- **Fix shipped:** `getattr(provider, "extra_config", None) or {}).get("disable_long_context") is True` at all 4 dispatch sites.
- **Pin:** `test_v5021_disable_long_context.py::test_dispatch_tolerates_mock_provider_without_extra_config` + `test_dispatch_sites_use_getattr_not_attribute_access`.
- **Status:** **fixed** (v5.0.21 hotfix shipped 2026-06-05 PM).

#### BUG-050 — `bool("false") == True` silent flag inversion
- **Component:** Same 4 dispatch sites as BUG-049.
- **Severity:** critical (silent inversion of operator intent on a billing-relevant flag).
- **Repro:** `python3 -c 'print(bool("false"))'` → `True`. If `extra_config.disable_long_context = "false"` (string, e.g. operator sends JSON via REST), the proxy interprets it as `True`.
- **Cause:** `bool(...)` truth-test on any non-empty string returns `True`.
- **Fix shipped:** Replaced `bool(x)` with `x is True` at all 4 sites.
- **Pin:** `test_v5021_disable_long_context.py::test_dispatch_sites_use_identity_check_not_bool`.
- **Status:** **fixed** (v5.0.21 hotfix shipped 2026-06-05 PM).

#### BUG-051 — v5.0.18 frontend `clusterApi` uses wrong path → cluster_peers UI DOA
- **Component:** `frontend/src/api/index.ts:423-426`.
- **Severity:** critical (operator-facing feature broken from day one; the entire purpose of v5.0.18).
- **Repro:** `curl https://www.voipguru.org/llm-proxy2/api/cluster/peers` → **404**. The actual route is at `/cluster/peers` (no `/api/` prefix); every other `clusterApi.*` method uses `/cluster/*` correctly.
- **Cause:** typo in v5.0.18 frontend additions — copy-paste pattern from a different route.
- **Fix shipped:** changed all three paths to `/cluster/peers`.
- **Pin:** TODO — add an integration test that calls `clusterApi.listPeers()` against a running stack to catch path drift.
- **Status:** **fixed** (v5.0.18-hotfix shipped 2026-06-05 PM).

### Critical — OPEN

#### BUG-052 — Grok bridge concurrent `/api/chat` race corrupts responses
- **Component:** `grok_bridge/app.py` (`chat()` + `_send_via_spa_ui`).
- **Severity:** critical (two concurrent chats can return swapped or unrelated content; provider routing under load is unsafe).
- **Repro:** Fire two `/api/chat` calls with distinct user messages in parallel against the same `conversation_id`. Pre-fix observation 2026-06-05:
    - Call A (sent "Reply with just the letter A") → `{"detail":"grok.com 599: SPA-UI: chat-submit button not found"}`
    - Call B (sent "Reply with just the letter B") → `"Hi Grok-Web-Devin! 👋 Streak still going strong. What's the next test or idea you've got lined up?"` (unrelated content)
- **Cause:** `_send_via_spa_ui` mutates shared `_page` state (textarea typing, button click, response listener) without acquiring `_lock`. The single Chromium tab interleaves the two requests.
- **Fix:** Wrap `_send_via_spa_ui` body in `async with _lock:`. Confirm `chat()` and `create_new_conversation()` both hold the lock around the SPA-driven path. Consider a per-conversation queue if higher concurrency is needed.
- **Status:** **open**, P0 next release.

#### BUG-053 — Cursor bridge silently swallows account-downgrade errors as HTTP 200
- **Component:** `llm-proxy2-cursor-bridge` + `app/providers/cursor_bridge_*` (caller-side handling).
- **Severity:** critical (provider returns empty content on a real failure; routing layer can't tell, callers see `success` with empty payload).
- **Repro:** `curl -X POST https://www.voipguru.org/llm-proxy/v1/messages -H 'x-api-key: <devingpt key>' -d '{"model":"claude-haiku-4-5","messages":[{"role":"user","content":"Say PONG"}],"max_tokens":20}'` → `{"content":[{"type":"text","text":""}], "usage":{"input_tokens":0,"output_tokens":0}}`. Three consecutive retries all empty.
- **Evidence (cursor-bridge log):** `{"error":{"code":"resource_exhausted","details":[{"debug":{"error":"ERROR_RATE_LIMITED_CHANGEABLE","details":{"title":"Named models unavailable","detail":"Free plans can only use Auto. Switch to Auto or upgrade plans to continue."}}}]}}` followed by `POST /v1/chat/completions 200 286`.
- **Cause:** Cursor's API returned a structured error (account downgraded to free plan), but cursor-bridge converted it to HTTP 200 OK with the error JSON embedded; the proxy interpreted 200 as success and forwarded empty content to the caller.
- **Fix:** (1) cursor-bridge: detect `code: "resource_exhausted"` / `ERROR_RATE_LIMITED_CHANGEABLE` and return a real 429 with `Retry-After`; (2) proxy: when content is empty + finish_reason `stop` + 0 token usage, treat as upstream failure and trigger failover; (3) cursor billing scrape: detect plan-tier changes (Pro→Free) and auto-disable the provider with operator notification.
- **Status:** **open**, P0 next release. Workaround: operator should manually disable Cursor-oAuth-C1acct until the account is upgraded or the bridge is patched.

### High — OPEN

#### BUG-054 — Bridge `_send_via_spa_ui` early returns leak response listeners
- **Component:** `grok_bridge/app.py:1381, 1418` (paths in `_send_via_spa_ui`).
- **Severity:** high (dangling listener fires on the NEXT request's `/responses` POST, resolving the wrong future with the wrong body).
- **Repro:** Trigger any path that returns early ("no usable textarea found", "chat-submit not found"). The `_page.on("response", _on_response)` is installed BEFORE the early-return branches; cleanup is only in the `finally` clause, which the early returns bypass.
- **Cause:** Early returns not refactored to flow through `try/finally`.
- **Fix:** Wrap the SPA-UI body in `try/finally` such that ALL exits remove the listener. Either reorder (install listener AFTER typing succeeds) or replace early `return`s with raising-then-handle.
- **Status:** **open**.

#### BUG-055 — `_cookie_refresh_loop` races with `/api/chat` page.goto
- **Component:** `grok_bridge/app.py:382` (`_cookie_refresh_loop`).
- **Severity:** high (deterministic chat failure within ~30s of every 25-min refresh tick).
- **Repro:** Trigger any `/api/chat` exactly when the refresh loop fires. The loop's `_page.goto(GROK_BASE + "/")` navigates AWAY from `/c/<conv_id>`; chat lands on root, no textarea, returns 599.
- **Fix:** Refresh loop must acquire `_lock` around its `_page.goto`. Alternative: dedicated refresh-only page in a separate tab.
- **Status:** **open**.

#### BUG-056 — Stray `docker-compose.yml` at repo root masks canonical `/home/dblagbro/docker/docker-compose.yml`
- **Component:** `/home/dblagbro/llm-proxy-v2/docker-compose.yml` (repo file).
- **Severity:** high (silent deploy failures — repeated `no such service: llm-proxy` errors during the sweep when commands ran from the repo dir; some deploys appeared to succeed without actually recreating the clone).
- **Repro:** `cd /home/dblagbro/llm-proxy-v2 && sudo docker compose up -d --force-recreate --no-deps llm-proxy` → `no such service: llm-proxy`. The repo `docker-compose.yml` defines only `llm-proxy2` + its volumes; the clone (`llm-proxy`) and bridges live in `/home/dblagbro/docker/docker-compose.yml`.
- **Fix:** Either delete the repo-root compose file (no longer needed if it was for local development) or rename it to make ambiguous compose-file resolution obvious (e.g. `docker-compose.dev.yml.example`). Document the canonical compose location in `CLAUDE.md`.
- **Status:** **open** (ops hygiene; immediate workaround is `cd /home/dblagbro/docker` before every compose command).

#### BUG-057 — `cluster_peers` self-row phantom on `CLUSTER_NODE_ID` change
- **Component:** `app/cluster/sync_handlers.py::_apply_cluster_peers` + `app/cluster/manager.py`.
- **Severity:** high (a config-edit + restart can leave an orphan self-row in `cluster_peers` table that the local manager treats as a peer of itself — infinite push loop or misdirected traffic).
- **Repro:** With `CLUSTER_NODE_ID=A` running, add A as a peer to cluster (UI). Stop the container, change `CLUSTER_NODE_ID=B`, restart. The row keyed by `A` is no longer filtered as self; manager pushes sync payloads to `A`'s URL (which is THIS node).
- **Fix:** On startup, after seeding, run a pruning pass: `DELETE FROM cluster_peers WHERE id = ?` for the current `cluster_node_id`. Also reject `POST /cluster/peers` with an id that matches the current node (already done — `cluster.py:385`), but the rename case still bypasses that check.
- **Status:** **open**.

### Medium — OPEN

#### BUG-058 — `/api/diagnostic/capture_next_send` doesn't hold `_lock`; listener collisions with concurrent chats
- **Component:** `grok_bridge/app.py:476`.
- **Severity:** medium (diagnostic endpoint can capture another in-flight chat's response or starve the chat's listener).
- **Fix:** Either acquire `_lock` (which serializes capture with chats), or document the trade-off and let operator coordinate.
- **Status:** open.

#### BUG-059 — Statsig validator false-positives on random base64 statsigs (~0.016%)
- **Component:** `grok_bridge/app.py::_statsig_id_looks_valid`.
- **Severity:** medium (occasional rejection of valid statsigs → re-capture latency).
- **Fix:** Tighten to exact prefix match (e.g. `x0:Type`, `x0:Reference`) rather than substring `"error"` / `"Type"`.
- **Status:** open.

#### BUG-060 — Statsig cache TTL is fixed at 600s; rotation cadence unknown
- **Component:** `grok_bridge/app.py:548`.
- **Severity:** medium.
- **Fix:** Measure empirically (sample 50 statsigs over a day, infer rotation period); adjust TTL to 0.5× observed period, or invalidate on first 403.
- **Status:** open.

#### BUG-061 — `_reload_peers_from_db` swaps `_peers` without lock
- **Component:** `app/cluster/manager.py`.
- **Severity:** medium (race window is small but real — heartbeats during the swap can see partial state).
- **Fix:** Add `asyncio.Lock` around the swap.
- **Status:** open.

### Low — OPEN

#### BUG-062 — `/cluster/peers` POST allows `http://` URLs
- **Component:** `app/api/cluster.py:387` — only checks `"://" in url`.
- **Severity:** low (cluster sync traffic could be unencrypted).
- **Fix:** Enforce `https://` prefix.
- **Status:** open.

#### BUG-063 — Frontend `confirm()` not Playwright-friendly
- **Component:** `frontend/src/pages/ClusterPage.tsx:295`.
- **Severity:** low (UI tests can't easily exercise removal path).
- **Fix:** Use a modal component (most of the codebase already has one for delete confirmations).
- **Status:** open.

#### BUG-064 — `_parse_iso_keep_naive` returns raw input on type mismatch
- **Component:** `app/cluster/sync_handlers.py`.
- **Severity:** low (a peer pushing `added_at: 123` int causes downstream comparison to crash; caught by per-section try/except so end-to-end behavior is just "this section skipped").
- **Fix:** Return `None` on unrecognized types.
- **Status:** open.

#### BUG-065 — Context-level listener leak on bridge shutdown
- **Component:** `grok_bridge/app.py` lifespan teardown.
- **Severity:** low (no functional impact in current single-context design; architectural risk).
- **Fix:** `_context.remove_listener("request", _on_context_request)` in lifespan `finally`.
- **Status:** open.

#### BUG-066 — Silent no-op when `CLUSTER_PEERS` env edited but DB already populated
- **Component:** `app/cluster/manager.py::_seed_peers_from_env_if_empty`.
- **Severity:** low (operator surprise — env edit assumed to take effect).
- **Fix:** Log `INFO` when env value differs from DB rows; document in `CLAUDE.md`.
- **Status:** open.

#### BUG-067 — Misleading retry comment in bridge `chat()`
- **Component:** `grok_bridge/app.py:1532`.
- **Severity:** low (documentation only).
- **Fix:** Remove or correct the comment.
- **Status:** open.

#### BUG-068 — `_send_via_spa_ui` and `create_new_conversation` UI-send code duplication
- **Component:** `grok_bridge/app.py`.
- **Severity:** low (maintenance burden).
- **Fix:** Extract a shared `_drive_spa_send(conv_id, message, on_response)` helper.
- **Status:** open.

### Coverage gaps surfaced

- **`v5.0.18` UI flow:** zero Playwright tests for ClusterPeersPanel add/remove/restore. Would have caught BUG-051 before deploy.
- **`v5.0.19/20` bridge SPA-UI:** zero unit OR integration tests for `_send_via_spa_ui`. The 4 concurrency/race bugs (BUG-052, 054, 055, 058) would have been caught by a 2-call parallel test against a mocked Playwright.
- **`v5.0.21` ContextVar plumbing:** added `test_v5021_disable_long_context.py` in this sweep (8 pins) — covers source/behavior contracts.
- **Cursor bridge error mapping:** no tests verify that Cursor error JSON shapes get translated to non-200 statuses. BUG-053 would have been caught by a fixture test feeding the captured error JSON.
- **Cluster-peers sync end-to-end:** 1 unit test added in v5.0.18 (`test_v5018_cluster_peer_persistence.py` — 7 pins). No integration test exercises the full path through two real nodes.

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
- **Fix shipped (v3.10.12)**: `messages.py` captures `_route_pre_dispatch` before the dispatch call; if `route` changed afterward it pops the old route's `litellm_kwargs` keys from `extra` and applies the new route's — so the litellm dispatch uses the fallthrough provider's credentials/base_url/headers. (`system`/`tools` are request-derived and unchanged by a route swap, so they don't need rebuilding.)
- **Status**: fixed in v3.10.12 — rare path; runtime confirmation still recommended (deliberately 401 every claude-oauth provider).

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
- **Fix shipped (v3.10.14)**: the write side was already wired — `main.py` reads the `X-Internal-Source` header into a request ContextVar and `record_outcome` records `event_meta.internal_source`. The gap was purely the read side: `compute_provider_stats` and `observability_sampler._sample_error_rate` now **skip** rows where `event_meta.internal_source` is set (same pattern `ai_rate_limiter` already used). The supervisor no longer counts its own classifier calls against the provider it judges, and internal traffic no longer feeds the error-rate alert.
- **Status**: fixed in v3.10.14

### BUG-027 [MEDIUM] Integration test `test_release_now_also_enables_v386` fails deterministically

- **Area**: `tests/integration/test_manual_override_flow.py` / manual-override "release all to AI control" flow (v3.8.6)
- **Repro**: `pytest tests/integration/test_manual_override_flow.py::test_release_now_also_enables_v386`
- **Actual**: deterministic failure (re-ran twice). Not yet root-caused — could be a v3.8.6 behaviour regression or environmental (depends on current provider override state on the cluster).
- **Recommended fix**: triage — capture actual vs expected; determine regression vs environment.
- **Triage (v3.10.12)**: **environmental, not a product regression.** The test asserted a pre-staged precondition (`Devin-Anthropic-Max-VG` = `enabled=False` + locked) that it never established itself, and on success left that *live production* provider disabled. The provider is currently `enabled=True` (correctly), so the precondition assertion failed deterministically. The v3.8.6 release feature itself is fine.
- **Fix shipped (v3.10.12)**: `test_release_now_also_enables_v386` now self-stages its own precondition (toggles the canary to disabled+locked if needed) and restores the provider's **original** state at the end — no external-state dependency, no destructive side-effect. Cannot be run in the no-browser QA env; **pending verification on the next www1 Playwright run**.
- **Status**: fixed (test hardened) in v3.10.12 — pending Playwright re-run on www1.

### BUG-028 [MEDIUM] Cross-family translator still mishandles two message shapes

- **Area**: `app/api/_oauth_chat_translate.py`
- **Detail**: beyond the v3.10.0 fix — (a) an Anthropic assistant block with no text and no tool_use translates to `{"role":"assistant","content":null}` with no `tool_calls`, which OpenAI rejects; (b) `tool_result` → `role:"tool"` is emitted without verifying it *immediately follows* the matching assistant `tool_calls` — a misordered (not orphaned) pair still produces an OpenAI 400. The `known_tool_use_ids` pre-scan only catches fully-orphaned ids.
- **Evidence**: code audit.
- **Recommended fix**: emit a placeholder for empty assistant blocks; validate tool-message adjacency (or reorder) in `anthropic_messages_to_openai`. Add regression tests with both shapes.
- **Fix shipped (v3.10.12)**: (a) an assistant turn with neither text nor tool_use now emits a `_EMPTY_ASSISTANT_CONTENT_PLACEHOLDER` string instead of `content:null` + no `tool_calls`. (b) the global `known_tool_use_ids` pre-scan is replaced by **adjacency tracking** — a `tool_result` becomes a `role:"tool"` message only if its id was declared by the *immediately preceding* assistant turn; orphaned OR misordered/cross-turn `tool_result`s degrade to plain user text. 5 regression tests in `tests/unit/test_v31012_buglog_fixes.py`.
- **Status**: fixed in v3.10.12

### BUG-029 [MEDIUM] `/lmrh/quotes?model=<unknown>` returns 200 with empty `model_id` instead of an unknown-model error

- **Area**: `app/api/lmrh_v2.py`
- **Repro**: `GET /lmrh/quotes?model=this-model-does-not-exist-xyz` (auth'd)
- **Expected**: 4xx / explicit "unknown model" so a caller pre-flighting a typo gets a true signal
- **Actual**: 200 with `candidates:[{"model_id":"","score":888.0,...}]` — silently falls back to auto-routing the default provider.
- **Recommended fix**: when no capability matches the requested model, return 404/422 with a clear message.
- **Fix shipped (v3.10.14)**: `/lmrh/quotes` does **not** 404 — that would make the pre-flight lie, since `/v1/messages` *substitutes* an unknown model (operator decision under BUG-037). Instead the response now carries `requested.model_recognized` (bool) and a `warnings[]` list — an unregistered model id gets an explicit "not a registered model id — candidates reflect substitution/auto-routing" signal while still returning the substituted candidates.
- **Status**: fixed in v3.10.14

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
- **Fix shipped (v3.10.15)**: a `logging.Handler` (`app/observability/infra_error_tap.py`) is attached to the `sqlalchemy.pool` (WARNING+) and `uvicorn.error` (ERROR+) loggers. It does not re-log — it classifies each record (`fault_class=disconnect` for benign client-disconnects vs `fault` for genuine faults) and increments `llm_proxy_infra_errors_total{source,fault_class}`. Infra errors are now visible on `/metrics`, and the observability sampler's new `_sample_infra_errors()` logs a warning when genuine faults climb ≥5 per 30s tick. The handler only touches an in-memory counter (no DB write from a pool-error context).
- **Status**: fixed in v3.10.15

### BUG-033 [LOW] Orphan `tool_result` with image content silently drops the image

- **Area**: `app/api/_oauth_chat_translate.py::_tool_result_content_to_str`
- **Detail**: an orphaned `tool_result` whose content is an image block is flattened to the literal `"[image]"` — the image payload is silently discarded with no caller-visible signal.
- **Recommended fix**: at minimum document it; ideally translate the image to an OpenAI `image_url` part.
- **Fix shipped (v3.10.14)**: `_tool_result_content_to_str` now emits a descriptive marker — `[image omitted: <media_type> — OpenAI tool-role messages cannot carry image content]` — instead of a silent `[image]`. The image is still dropped (OpenAI `role:"tool"` messages genuinely cannot carry image parts), but it is now **visible** to the caller, not silent. Full preservation (promoting the tool_result to a user-message image part) is a larger change, tracked separately.
- **Status**: fixed in v3.10.14 (made visible; full image preservation deferred)

### BUG-034 [LOW] Inconsistent auth-error wording + `/lmrh/quotes` status inconsistency

- **Detail**: no-key responses say `"Missing API key"` on `/v1/messages` but `"missing api key"` (lowercase) on `/v1/models` and `/lmrh/*` — two `verify_api_key`/`resolve_api_key_dep` paths with divergent copy. `/lmrh/quotes` with a missing `model` → 422; with empty `model=` → 400 — same logical failure, two shapes; the `if not model` branch is partly dead (FastAPI rejects a missing required query first).
- **Recommended fix**: unify the auth-error string; pick one status for missing/empty `model`.
- **Fix shipped (v3.10.10)**: `resolve_api_key_dep` now raises `"Missing API key"` (was lowercase `"missing api key"`) — matches `verify_api_key`, so `/v1/models` and `/lmrh/*` no-key responses are consistent with `/v1/messages`.
- **Fix shipped (v3.10.14)**: `/lmrh/quotes` `model` param is now `Optional[str] = None` — a *missing* `model` and an *empty* `model=` both hit the same `if not model` → **400**, the one consistent shape (was 422 vs 400).
- **Status**: fixed in v3.10.14 — auth wording unified (v3.10.10) + `/lmrh/quotes` status shape unified (v3.10.14). Closed.

### BUG-035 [enhancement] `/v1/embeddings` Pydantic `list[float]` vs base64-`str` serializer warnings

- **Detail**: every `/v1/embeddings` call logs `PydanticSerializationUnexpectedValue` — the `embedding` field is declared `list[float]` but receives a base64 `str`. Response is 200; this is per-request log noise from a response-model mismatch.
- **Recommended fix**: widen the response model to `list[float] | str` (or split by `encoding_format`).
- **Re-assessment (v3.10.14)**: the tractable part is **already done** — v3.7.19 (BUG-021) made the embeddings handler call `result.model_dump(warnings="none")` and decode base64 vectors to `list[float]`. The proxy's own route has no `response_model`, so it emits no warning. Any residual `PydanticSerializationUnexpectedValue` originates **inside litellm's** `EmbeddingResponse` serialization — not our code; not fixable without patching litellm. No further proxy-side change is warranted.
- **Status**: wont-fix (proxy side) — tractable part shipped in v3.7.19; residual is litellm-internal.

### BUG-036 [enhancement / hardening] `_messages_dispatch.py` (v3.10.9 refactor) has no behavioral test coverage

- **Area**: `tests/unit/test_v3109_messages_dispatch_extract.py`
- **Detail**: the v3.10.9 extraction moved the proxy's deepest hot path (claude-oauth chain walk, 401-refresh fallback, streaming pre-flight, empty-stream→502, network-error→next-provider, fallback-exhaustion) into `_messages_dispatch.py` (256 lines). The test file has 4 tests — 3 are source-grep wiring checks, 1 is behavioral but exercises only the trivial "route is not claude-oauth → fall through" path. **Zero behavioral coverage** of any dispatch branch. A "behaviour-preserving move" with no behavioural assertions cannot prove behaviour was preserved.
- **Recommended fix**: add mocked-chain tests for: 401→refresh→retry, network-error→next-provider, empty-stream→502, fallback-exhaustion→HTTPException, streaming pre-flight failure.
- **Fix shipped (v3.10.15)**: `tests/unit/test_v31015_buglog_fixes.py` adds 8 genuine behavioral tests for `dispatch_claude_oauth_chain` — non-oauth fall-through, oauth success→JSONResponse, 401→fallover, network-error→fallover, fallback-exhaustion→HTTPException, streaming pre-flight HTTP error→HTTPException, empty-stream→502, streaming success→StreamingResponse. The cache/disclosure/memory collaborators are mocked so each test exercises the chain-walk logic in isolation. Every dispatch branch now has a behavioral assertion.
- **Status**: fixed in v3.10.15

### BUG-037 [HIGH] `/v1/messages` for an unregistered model id can hang ~40s+ (300s server-side ceiling)

- **Area**: model routing / substitution + `app/api/_messages_streaming.py` (`_CLAUDE_OAUTH_TIMEOUT`)
- **Discovered**: v3.10.10 re-investigation of the `test_revoke_key_rejects_llm_calls` failure (originally misfiled as BUG-023).
- **Repro**: `POST /v1/messages` with a **valid** key for `model: claude-3-5-sonnet-20241022` — a model with **no `ModelCapability` rows** on this cluster. Probe result: request hung and the client timed out at 40s (it had not completed). A second observation routed the same model to `OpenRouter → openai/gpt-4o` and succeeded — i.e. the substitution target is non-deterministic, and at least one target path hangs.
- **Expected**: an unroutable / unregistered model id should fast-fail with a 4xx, or route to a working provider within a normal completion time.
- **Likely cause**: with no capability rows the router substitutes a provider; `_CLAUDE_OAUTH_TIMEOUT` carries a **300s read timeout** (`httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)`). If the substitute is a claude-oauth provider and the upstream hangs on the unrecognised model, the proxy can wait up to 5 minutes before failing.
- **Why it matters**: (1) a single hung request holds its connection — and DB session — for up to 300s; under load this is a plausible **contributor to ARCH-A** (pool exhaustion). (2) the integration suite's revocation test flapped on this, not on auth.
- **Recommended fix**: (a) fast-fail (400/404 "model not available") when a model id resolves to no capability and no deterministic route; (b) tighten the claude-oauth `read` timeout from 300s to a sane ceiling (e.g. 120s) — needs deliberate review as it touches the streaming hot path. NOT bundled into v3.10.10 (out of the "quick wins" scope).
- **Partial fix shipped (v3.10.12)**: the claude-oauth timeout is now **split** — `_CLAUDE_OAUTH_STREAM_TIMEOUT` (streaming) has `read=120s`, `_CLAUDE_OAUTH_TIMEOUT` (non-streaming) keeps `read=300s`. Streaming `read` is the gap *between* chunks, so 120s is a safe ceiling that bounds a hung stream to 2 min (was 5) with zero risk to real traffic. Non-streaming `read` is effectively the whole-generation budget, so it is left generous.
- **Non-streaming fix shipped (v3.10.13)**: the non-streaming claude-oauth `read` timeout is now **scaled to the request's `max_tokens`** (`_oauth_complete_timeout`) — ~90s for a tiny request (e.g. the unregistered model id that hangs), up to the 300s ceiling for a genuinely large generation. Bounds the non-streaming hang for the observed case with zero risk to real large completions.
- **Operator decision (2026-05-16)**: keep model substitution — do **not** add an outright unknown-model rejection. When a substituted model can't support a requested feature (e.g. native tool calling) the proxy already emulates it via prompt modification / prompt add-ins; monitor that emulation path and fix issues as they surface. Both hang paths are bounded (streaming ≤120s, non-streaming ≤300s, ~90s for small requests), which was the real defect.
- **Status**: fixed in v3.10.13 — hang paths bounded; substitution kept by operator decision. Closed.

### BUG-038 [MEDIUM] CoT streaming path skipped caller-memory write-back

- **Area**: `app/api/_messages_streaming.py::_stream_cot_anthropic`
- **Discovered**: v3.10.11, investigating DevinGPT's "extract metric at zero" report.
- **Detail**: streaming caller-memory write-back (v3.9.11 / v3.9.14) wired `maybe_extract_memory_writes` into `_stream_claude_oauth` and `_stream_anthropic`, but **not** `_stream_cot_anthropic` (the CoT iterative-refinement streaming path). A memory-enabled request that engaged CoT would `inject` on the request side but never `extract` on the response side — a silent half-loop.
- **Fix shipped (v3.10.11)**: `_stream_cot_anthropic` now accepts `conversation_id`/`memory_tag`, accumulates memory-tool `tool_use` blocks from the SSE passthrough (keyed by content-block index), and feeds the assembled response through `maybe_extract_memory_writes` once the stream completes — same contract as the other two streaming paths. `messages.py` threads the params via `extra_kwargs_for_stream`. 3 behavioral tests in `tests/unit/test_v31011_cot_memory_writeback.py`.
- **Status**: fixed in v3.10.11

### ARCH-A — ROOT-CAUSED + FIXED (v3.10.13)

The latent DB connection-pool leak (open since the v3.9.15 sweep).
- **Diagnosis (2026-05-16)**: a live `/cluster/db-pool-trace` capture
  caught **one** connection checked out for **3864s** (~64 min) — and
  it was **www01-only** (www02 + GCP pools clean). The one thing
  enabled only on www01 is the **AI provider supervisor**. Root cause:
  `ai_provider_supervisor.review_one_provider` ran
  `compute_provider_stats` (which checks out a pooled connection on its
  ORM query) and then `await classify_with_llm(...)` — an httpx LLM
  call — **without releasing the connection first**. The session's
  connection was pinned across every classification call for the whole
  multi-provider scan; under a slow/stuck await it was held
  indefinitely. This is exactly the v3.9.15 sweep's hypothesis #2 — "a
  long-lived task holding a session across a hung await."
- **Fix shipped (v3.10.13)**: `review_one_provider` now `await db.commit()`s
  immediately after reading stats and **before** `classify_with_llm`,
  returning the connection to the pool for the duration of the LLM
  call (`expire_on_commit=False` keeps `provider` usable). The review
  write afterward checks out a fresh connection. Behavioral test:
  `test_v31013_buglog_fixes.py::test_review_one_provider_releases_db_before_llm_call`.
- **Tooling fix (v3.10.13)**: the pool tracer captured only the last
  18 stack frames — too shallow (SQLAlchemy's checkout chain is ~16
  frames), so the trace showed only ORM internals, never the app
  caller. Bumped to 45 frames so future captures name the path
  directly.
- Tracer (`DB_POOL_TRACE=1`) is enabled on all 3 nodes.
- **Status**: fixed in v3.10.13 — verify pool stays flat >24h post-deploy.

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

### BUG-001 — streaming error contract — FIXED (v3.10.13)

- **Subsystem**: `_messages_streaming.py`, `_completions_streaming.py`
- **Root cause**: the litellm streaming dispatch returned HTTP 200 (the
  first SSE chunk had already left), then emitted a terminal
  `data: {"type":"error"}` + `message_stop` when upstream failed.
  Clients that check `r.status_code` saw success and an empty stream.
- **Why it was deferred**: a wire-contract change looked like it needed
  DevinGPT + hub sign-off. Re-analysis closed that concern: the
  claude-oauth streaming path **already** pre-flights (returns a real
  HTTP status on a pre-stream error, since v2.7.6 BUG-018), and DevinGPT
  routes claude-family traffic through that path — so it already sees
  shape #1. The litellm path was simply inconsistent.
- **Fix shipped (v3.10.13)**: `preflight_sse()` pulls the first SSE
  frame before the `StreamingResponse` is constructed. If that frame is
  a terminal error event (Anthropic `{"type":"error"}` *or* OpenAI
  `{"error":...}` shape), the request raises an `HTTPException` with a
  real status (`http_status_for_stream_error` → 401/429/502) instead of
  a 200 + error-frame. A successful first frame is replayed, then the
  rest of the stream. Applied to the plain `/v1/messages` and
  `/v1/chat/completions` litellm streaming paths. **Mid-stream** errors
  (after `message_start`) still degrade to an SSE error frame — the 200
  is already committed; that is unavoidable and unchanged.
- **Hedged path (v3.10.16)**: the hedged streaming path now pre-flights
  too — `messages.py` / `completions.py` run `preflight_sse` on the
  `race_streams` racer, so a pre-stream failure on the winning branch
  raises a real HTTP status (parity with the non-hedged path).
- **Hedge-correctness fix (v3.10.17)**: `race_streams` previously
  treated an error-frame first chunk as a race "win" — a fast-failing
  primary could beat a healthy backup. It now classifies the first
  chunk: an error frame / empty stream counts as a FAILURE, not a win,
  so a healthy backup wins over a failing primary. If both branches
  fail, primary's failed stream is returned so `preflight_sse` still
  surfaces a real status.
- Tests: 8 in `test_v31013_buglog_fixes.py`, 2 hedged-path in
  `test_v31016_buglog_fixes.py`, 5 race-correctness in
  `test_v31017_buglog_fixes.py`.
- **Status**: fixed — v3.10.13 (plain) + v3.10.16 (hedged pre-flight)
  + v3.10.17 (hedge race-correctness). Fully closed.

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

---

# Archived — QA-pass findings (merged from `docs/bug-log.md`, 2026-08-17)

These sections lived in a second `bug-log.md` under `docs/` that shared the name but
not the content: the two files were **fully disjoint** (10 sections here, 14 there, zero
overlap). This half is the QA-pass finding stream from the v3.5.7 - v4.4.28 era; the half
above is the regression-sweep stream. Merged so there is one bug log, per AGENTS.md.

Original preamble: findings from the 2026-05-09 deep QA pass on llm-proxy2 v3.5.7.
Same severity ladder and status flow as above.

## 2026-05-29 — coordinator-hub overcap on cache_control markers

### BUG-085 — Caller sending 5 cache_control markers (Anthropic caps at 4) — ⚠ **OBSERVED + LOG ADDED v4.4.29 (hub-side root cause)**

- **Severity:** medium · **Category:** cross-team (proxy clean; needs hub-side fix)
- **Surfaced:** 2026-05-29 during post-deploy fleet health check after v4.4.27/.28 went out. Activity-log severity in the last 3h: 6361 info / 20 warning / **17 error** (vs the 0 errors seen in the previous QA pass).
- **Repro:** all 16 `llm_request` errors in the 3h window are `error_class=bad_request` from `Devin-Anthropic-Max-VG` (claude-haiku), with upstream message `400: ... "A maximum of 4 blocks with cache_control may be provided. Found 5."` Daily counts: 2026-05-20: 1 (one-off), **2026-05-29: 14** (clearly an active issue today).
- **Root cause:** the **coordinator-hub** caller is sending requests with 5 `cache_control` markers. The proxy's `_inject_claude_code_system` (`app/api/_messages_streaming_oauth.py:117`) already caps its own injection — when the caller's count is already 4+, the proxy adds its marker WITHOUT `cache_control`. So the 5 is entirely caller-supplied. Related to the hub team's F2 work (BUG-082 territory) where they added cache breakpoints to their Avaya enricher template.
- **Body-sample confirmation:** the v3.9.2 4xx body-sampler captured 1 of the failing bodies. Body is 3992 chars (truncated at the `activity_log_max_body_chars=4000` cap). First chars show `model=claude-haiku-4-5-20251001, messages[0].content=[…]` — `messages` opens the body and the cache_control markers are further in, past the truncation point. Direct count from the sample wasn't possible.
- **Fix shipped (proxy observability):** v4.4.29 adds a `logger.warning` in `_inject_claude_code_system` whenever the caller's `cache_control` count is > 4. Logs the breakdown `sys=N msgs=N tools=N` so the source location of the excess markers is immediately obvious from the next occurrence. Confirms the proxy is not adding the 5th: "Proxy did NOT add its own marker's cache_control."
- **Hub-side fix:** the hub team needs to audit their template — they probably have `cache_control` on multiple system blocks + multiple message blocks. Anthropic's cap is 4; they need to trim. Bundles with the BUG-082 memo follow-up (the F2 work is the source of both findings).
- **Tests** (`tests/unit/test_v4429_cache_marker_overcap_log.py`, +4 of 5): source guards (BUG-085 reference, breakdown fields, telemetry try/except), behavioral (warning fires at 5 with right sys/msgs/tools counts, no warning at ≤4).
- **Status:** OPEN — observability added; hub-side fix needed.

### F-INFRA-003 — Plaintext admin password committed in `tests/conftest.py` — ✅ **CLOSED v4.4.29**

- **Severity:** medium · **Category:** credential exposure (latent)
- **Repro:** `tests/conftest.py:15` previously read `ADMIN_PASS = "REMOVED-CREDENTIAL-ROTATED-20260828"` — a hardcoded production-admin password committed in git history on a public repository. Visible to anyone who clones or browses the repo.
- **Fix:** v4.4.29 reads `ADMIN_PASS` from `LLMPROXY_TEST_ADMIN_PASS` env var with a dev-default fallback of `"admin"`. Same env-var pattern applied to `BASE_URL` (`LLMPROXY_TEST_BASE_URL`) and `ADMIN_USER` (`LLMPROXY_TEST_ADMIN_USER`) for consistency. Operator sets these in their shell/.env when running integration tests against live; default-credentials dev boxes still work from a clean checkout.
- **Caveat (not in scope here):** the password remains in `git log -p tests/conftest.py` for the lifetime of the existing history. A full `git filter-repo`-style history rewrite is out of scope for this fix — the practical remediation is to rotate the password on the live deployment if it hasn't been already. Flagging for operator action.
- **Tests:** the test file's source-guard asserts `REMOVED-CREDENTIAL-ROTATED-20260828` is no longer present and that the env var name is wired in.
- **Status:** CLOSED v4.4.29 (forward-looking). Operator should rotate the password if it's still in active use anywhere.

## 2026-05-28 — QA-pass remediation arc COMPLETE (v4.4.24 → v4.4.28)

The 2026-05-27 findings below were remediated across 5 releases. Final status:

- **BUG-079** (cluster sync broken) — ✅ **CLOSED v4.4.24** (`.limit(1)` guard + de-dup data fix). **PERMANENTLY CLOSED v4.4.27** via `UNIQUE(provider_id, captured_at)` schema constraint. The race that wrote the duplicate is now schema-level impossible. Confirmed live: between v4.4.24 cleanup and v4.4.27 prep, www2 silently accumulated 3 NEW dup groups in 24h (race was still active under the guard). Post-v4.4.27 direct INSERT raises `IntegrityError`.
- **BUG-080** (5 vulnerable handlers) — ✅ **CLOSED v4.4.24** (`.limit(1)` on all 5). The two ai_review tables (provider_ai_review + api_key_ai_review) additionally got UNIQUE indexes in v4.4.27; the others stay belt-and-braces-protected by `.limit(1)`.
- **BUG-081** (push_sync ignores response) — ✅ **CLOSED v4.4.24.**
- **BUG-082** (F2 cache_control not engaging) — ✅ **PROXY EXONERATED v4.4.27 pass**: controlled 2-request probe confirmed prompt caching works end-to-end on the claude-oauth subscription path (cache_creation=16037 then cache_read=16037 on the same Devin-Anthropic-Max provider the hub uses). Root cause is HUB-side request structure (cacheable content in user message instead of system prefix). Memo presented for forward 2026-05-28.
- **BUG-083** (negative hours) — ✅ **CLOSED v4.4.24.**
- **BUG-084** (api_keys INSERT field coverage) — ✅ **CLOSED v4.4.25.**
- **F-INFRA-001** (non-hermetic unit suite) — ✅ **CLOSED v4.4.24.** Session-finish purge gated behind `LLMPROXY_TEST_PURGE_LIVE=1`.
- **F-OBS-004** (contrast) — ✅ **WORST CASE FIXED v4.4.26** (1.84 → 3.42 on the 10px "Anthropic Console" label). ✅ **TERTIARY DARK-MODE SWEPT v4.4.28** (29-file sweep, 3.03 → 3.42 across the bare `text-gray-500` labels). Remaining residual (light-mode `text-gray-400` description text + status-coded colors) operator-accepted as intentional design.
- **F-OBS-005** (a11y) — ✅ **FULLY CLOSED v4.4.26.** All 9 pages a11y-OK per Playwright audit.
- **F-INFRA-002** (Playwright stale assertion + timeouts) — still open, low priority.

## 2026-05-28 — QA-pass remediation (v4.4.24 + v4.4.25)

The 2026-05-27 findings below were remediated. Status updates (now superseded by the consolidated 2026-05-28 entry above; kept for traceability):

- **BUG-079** (cluster sync broken) — ✅ **CLOSED v4.4.24.** `.limit(1)` guard + de-dup data fix on www2/c1conv. Verified live: minted+PATCHed api_key on www1 propagated to both peers in <80s (the exact test that found it). Peer `/cluster/sync` returns 200, not 500.
- **BUG-080** (5 vulnerable handlers) — ✅ **CLOSED v4.4.24.** All 5 got `.limit(1)`. Source-guard test prevents regression on future handlers.
- **BUG-081** (push_sync ignores response) — ✅ **CLOSED v4.4.24.** Response status now inspected + logged on non-200.
- **BUG-083** (negative hours) — ✅ **CLOSED v4.4.24.** `Query(hours, ge=1, le=720)`.
- **F-INFRA-001** (non-hermetic unit suite) — ✅ **CLOSED v4.4.24.** Session-finish purge gated behind `LLMPROXY_TEST_PURGE_LIVE=1`.
- **F-OBS-004** (contrast) — DEFERRED to a visual-review session (blind sweep too risky; one existing `dark:` edge case would corrupt).
- **F-OBS-005** (a11y) — still open, enhancement.

### BUG-084 — api_keys cluster-sync INSERT path drops extended operator fields — ✅ **CLOSED v4.4.25 (2026-05-28)**

- **Severity:** medium · **Category:** confirmed defect · cluster-sync field coverage (insert path)
- **Surfaced:** 2026-05-28 during post-v4.4.24 verification of the BUG-079 fix. The row + LWW stamp propagated to peers (BUG-079 confirmed fixed), but `semantic_cache_enabled` arrived `0` and `daily_hard_cap_usd` arrived `NULL` — the PATCHed values were lost.
- **Root cause:** `apply_sync`'s api_keys INSERT path (`app/cluster/sync.py:168`) materialized only base columns (id/name/hash/prefix/type/enabled/spending_cap/rate_limit_rpm) + the v4.4.20 stamp. The 8 extended fields added to the UPDATE push/apply in v4.4.18 were never added to the INSERT. Compounding it: the insert sets `last_user_edit_at` to the origin's stamp, so the *next* sync hits the LWW tie (equal stamps → keep local) and the UPDATE path never backfills the extended fields. Net: a newly created+patched key's extended fields silently never reached peers unless the operator PATCHed a *second* time (bumping the stamp past the peer's).
- **Fix:** v4.4.25 adds all 8 extended fields to the INSERT path.
- **Tests** (`tests/unit/test_v4425_apikey_insert_field_coverage.py`, +3): source guard on insert coverage; behavioral repro (new key materializes extended fields); insert-then-tie consistency (second sync at equal stamp is a no-op, doesn't revert).
- **Status:** CLOSED v4.4.25.

---

## 2026-05-27 — Deep QA pass (post v4.4.23, fleet 3/3)

Comprehensive QA pass covering the v4.4.20→.23 release arc plus broader regression. Methodology: discovery → planning → test design → execution across unit + cluster contract + auth boundaries + LWW behavioral + schema + per-event header capture + async tracer + cron watchers + frontend (Playwright in-flight) + theme contrast spot. Result: **one HIGH severity (cluster sync silently broken)**, one MEDIUM (input validation), several low/hardening items. The HIGH finding is the headline — six days of cluster sync data divergence undetected by heartbeat-only health checks.

### BUG-079 — Cluster sync silently broken for ~6 days — `_apply_provider_ai_reviews` crashes on duplicate (provider_id, captured_at) — 🔴 **OPEN (HIGH)**

- **Severity:** **high** · **Category:** confirmed defect · data correctness across cluster
- **Surfaced:** 2026-05-27 during deep QA pass, by a live LWW behavioral test (created+PATCHed an api_keys row on www1, peers did NOT see it after 80s).
- **Repro:** mint a fresh api_key on www1 via the admin API, wait > 60s for the next sync cycle, query the peer's DB by `key_hash` — row absent. Confirmed by directly observing the peers' uvicorn logs:
  ```
  POST /cluster/sync HTTP/1.1 500 Internal Server Error
  File "/app/app/cluster/sync.py", line 859, in apply_sync
      await _apply_provider_ai_reviews(db, payload.get("provider_ai_reviews", []))
  File "/app/app/cluster/sync_handlers.py", line 152, in _apply_provider_ai_reviews
      )).scalar_one_or_none()
  sqlalchemy.exc.MultipleResultsFound: Multiple rows were found when one or none was required
  ```
- **Root cause:** `_apply_provider_ai_reviews` at `app/cluster/sync_handlers.py:152` queries `ProviderAiReview` by `(provider_id, captured_at)` using `.scalar_one_or_none()`. The table has **no UNIQUE constraint** on that pair and a check-then-insert race lets two writers (local AI-review + cluster-sync inbound apply) create duplicates concurrently. When apply_sync next walks the payload, the lookup raises `MultipleResultsFound`, the whole transaction rolls back, and **no rows in the payload are applied** (api_keys, providers, settings, blocked_ips, ai_reviews, caller_memory — everything).
- **Scope:**
  - www1: 0 duplicates → can apply incoming syncs fine
  - www2: 1 duplicate (provider_id=`da9fb8d610e5ccfa`, captured_at=`2026-05-21 12:08:09`)
  - c1conv: 1 duplicate (provider_id=`91bafda9cc28d0d6`, captured_at=`2026-05-25 07:02:06`)
  - **www1 → www2 sync: BROKEN** (since 2026-05-21, ~6 days)
  - **www1 → c1conv sync: BROKEN** (since 2026-05-25, ~2 days)
  - www2 → www1 sync: works (www1 has no dup)
  - c1conv → www1 sync: works
  - Effective topology: www1 acts as **read-only sink**, peers diverge for outbound changes
- **Implications:**
  - **v4.4.18 cluster-sync field coverage fix has been DEAD for the entire v4.4.18+ window.** Operator edits to api_keys on www1 (incl. the recent semantic_cache flip, daily caps, rate-tier changes) never reached peers.
  - **v4.4.20 LWW gate has been DEAD.** Schema migration ran per-node on deploy, so the *column* exists everywhere; field-level stamps don't propagate.
  - **v4.4.18+ patches that depended on cluster sync working** (LWW gate, ai-review propagation, blocked_ips replication) silently failed.
  - Schema-evidence: api_keys row counts diverge (www1=20, www2=20, c1conv=15); last_user_edit_at stamping diverges (www1=3, www2=0, c1conv=0).
  - **Heartbeat health is misleading** — peers report `status=healthy` because the heartbeat endpoint doesn't exercise apply_sync.
- **Suspected cause of the duplicate row itself:**
  - No UNIQUE constraint on `(provider_id, captured_at)` in `provider_ai_review`
  - The AI-review writer (`app/monitoring/ai_provider_supervisor*.py`) and the cluster-sync apply_handler both insert rows. If they race, both see no-existing → both insert → duplicate.
  - This is exactly the kind of check-then-insert race that `.limit(1)` defends against.
- **Related vulnerability pattern (BUG-080) — see below.**
- **Recommended fix direction:**
  1. **Hotfix code**: add `.limit(1)` to the `_apply_provider_ai_reviews` query (matches the v3.7.15 and v4.4-M2 defensive pattern in `_apply_external_usage_snapshots` and `_apply_provider_node_auth_states`).
  2. **Data fix**: hard-delete the older row of each duplicate pair on www2 + c1conv. Pick the row with NULL lifecycle fields (`applied_at`/`reverted_at`/`dismissed_at`) — that's the unused one.
  3. **Hardening**: add `UNIQUE(provider_id, captured_at)` constraint via ALTER (SQLite path: create new table + copy + drop + rename, since SQLite doesn't support adding unique constraints in place).
  4. **Observability gap**: surface apply_sync 500s on the originating peer. Currently `push_sync` in `cluster/manager.py:518-523` fires the POST but **does not inspect the response status**. A `r = await client.post(...)` + `r.status_code != 200` check + log line would have surfaced this immediately. Filed as separate observability hardening item below.
  5. **Tests**: add regression test that drives a duplicate row into `provider_ai_review` and asserts apply_sync still succeeds.
- **Status:** OPEN, **DO NOT FIX YET** per QA-pass protocol — pause for operator review.

### BUG-080 — 5 of 7 cluster-sync apply handlers share the same crash vulnerability — 🔴 **OPEN (HIGH)**

- **Severity:** **high** · **Category:** hardening / latent defect — same class as BUG-079 but not yet triggered
- **Surfaced:** 2026-05-27, during root-cause investigation of BUG-079. Audited every `scalar_one_or_none` call in `app/cluster/sync_handlers.py`.
- **Audit result:**
  | Line | Handler | Has `.limit(1)`? | Vulnerable? |
  |---|---|---|---|
  | 56 | `_apply_blocked_ips` | NO | latent risk on duplicate `ip` |
  | 108 | `_apply_ai_reviews` (api_key_ai_review) | NO | empty table now; risk if it grows |
  | **152** | `_apply_provider_ai_reviews` | **NO** | **ACTIVELY CRASHING** (BUG-079) |
  | 203 | `_apply_caller_memory` | NO | latent risk on duplicate (api_key_id, conv_id, tag) |
  | 251 | `_apply_caller_memory_markers` | NO | same |
  | 310 | `_apply_provider_node_auth_states` | **YES** | safe |
  | 352 | `_apply_external_usage_snapshots` | **YES** | safe (despite having actual duplicates) |
- **Recommended fix direction:** add `.limit(1)` to all 5 vulnerable queries. Also add UNIQUE constraints to the (provider_id, captured_at) / (api_key_id, captured_at) / (api_key_id, conversation_id, memory_tag) tuples where appropriate.
- **Status:** OPEN, do not fix yet.

### BUG-081 — `push_sync` does not inspect peer response status — 🟡 **OPEN (MEDIUM)**

- **Severity:** medium · **Category:** observability gap
- **Area:** `app/cluster/manager.py:517-523` (`push_sync`)
- **Repro:** the function calls `await client.post(...)` and never assigns the result. Peer 4xx/5xx responses are silently dropped. Only network-level exceptions are caught + logged.
- **Impact:** BUG-079 was undiscovered for ~6 days specifically because of this. Peers were returning 500 to every sync push; www1 logged nothing.
- **Fix direction:** assign the response, check `r.status_code != 200`, log a warning with `peer.id` + status + first-N bytes of response body. Same `_exc_str`-style fallback for empty error messages.
- **Tests:** unit-level — mock httpx client to return 500; assert a warning is logged.
- **Status:** OPEN, do not fix yet.

### BUG-082 — F2 cache-control not producing cache hits — ROOT-CAUSED to hub request structure (proxy verified clean) — 🟡 **OPEN (MEDIUM, cross-team) — memo drafted 2026-05-28**

- **Severity:** medium · **Category:** cross-team integration gap (NOT a proxy defect)
- **Surfaced:** 2026-05-27 11:00 EDT by the cron watcher `/home/dblagbro/bin/f2_cache_verify.sh`.
- **Verdict** (from `/home/dblagbro/log/f2_verify.verdict`): 479-req Avaya batch, zero cache_* fields.
- **Investigation 2026-05-28 (the "do BUG-082" pass):**
  1. **Volume check** — 4108 hub claude-haiku reqs in 48h; **2795 were ≥2048 tokens** (Haiku cache minimum). Size is NOT the problem. Zero of those 2795 had any cache token (`cc`/`cr` = `None`).
  2. **Zero cache_creation is the tell** — even a cache MISS writes (`cache_creation > 0`) when caching is active. Zero creation = caching not engaging at all on the hub's requests.
  3. **Routing** — hub claude-haiku → `Devin-Anthropic-Max-VG` / `Devin-Anthropic-Max-Gmail`, both `provider_type=claude-oauth` (subscription OAuth, not direct API).
  4. **Proxy path audit** — `_messages_streaming_oauth.py:117` preserves caller `cache_control` AND auto-injects an ephemeral marker on the system prompt when the caller hasn't. Beta bundle includes `prompt-caching-scope-2026-01-05`.
  5. **Controlled probe (decisive)** — sent 2 identical requests with a ~78 KB stable system prompt + explicit `cache_control` through the proxy to the SAME provider:
     - req1: `cache_creation=16037, cache_read=0` (wrote cache)
     - req2: `cache_creation=0, cache_read=16037` (full cache HIT)
     **Caching works end-to-end on this exact path.** The proxy is clean; the subscription endpoint honors caching.
- **Root cause (high confidence):** the hub's cacheable content is not behind a `cache_control` breakpoint on a **stable prefix**. Most likely the large per-article enrichment content sits in the **user message** (which varies per article, correctly uncached) while the small **system prompt** (where the proxy auto-injects the marker) is below the 2048-token cache minimum. So the cached prefix is too small to register.
- **Recommended fix direction (hub-side):** move the stable shared content (enrichment instructions + any shared reference material that repeats across articles) into a large **system prompt** block and place the `cache_control: {type: ephemeral}` breakpoint there. Per-article content stays in the user message (uncached — correct). Verify with the proxy's recorded `cache_creation`/`cache_read` on the next batch.
- **Optional proxy enhancement (not a bug, deferred):** the auto-inject currently always targets the system prompt. It could be smarter and place the marker on the largest stable block — but that's heuristic-heavy and risky; the hub-side fix is cleaner.
- **Status:** OPEN — memo drafted 2026-05-28 for operator to forward to hub team. Proxy exonerated.

### F-OBS-004 — DARK-mode contrast failures on `text-gray-500` / `text-gray-400 dark:text-gray-500` — ⚠ **MEASURED (low / a11y)** — corrected 2026-05-28

- **Severity:** low (a11y / readability)
- **Surfaced:** 2026-05-27 source-grep; **corrected 2026-05-28 by a Playwright audit measuring real WCAG ratios** via a canvas-based color resolver (the first audit pass used an rgb() regex that couldn't parse Tailwind-v4 `oklch()` serialization → false white-on-white ratio=1; that run's contrast results were discarded).
- **My original source-grep guess was WRONG.** The problem is NOT light-mode `text-gray-400` on white. The measured failures are in **dark mode** (the app's default theme), on `text-gray-500` and `text-gray-400 dark:text-gray-500`:
  | Page | Element | Class | Measured ratio | Need |
  |---|---|---|---|---|
  | api-keys | "Anthropic Console" (10px) | `text-[10px] text-gray-400 dark:text-gray-500` | **1.84** | 4.5 |
  | api-keys / providers / activity / users | sub-labels (12px) | `text-gray-500` (+ `dark:text-gray-400` variants) | **3.03** | 4.5 |
  | api-keys | "24h share · weekly utilization" (11px) | `text-[11px] text-gray-400 dark:text-gray-500` | **3.03** | 4.5 |
  | routing | count badge "10" | `bg-red-100` + red text | **3.46** | 4.5 |
  | several | section sub-headers (14px) | `text-sm text-gray-500` | **4.16** | 4.5 (borderline) |
  | metrics, cluster | — | — | **OK in both themes** |
- **Root insight:** `text-gray-500` (#6b7280) on the dark card bg (gray-900 #111827) ≈ 3.03:1. The `dark:text-gray-500` overrides are actively making dark mode *dimmer* than the light-mode `text-gray-400` they pair with — backwards. The 10px label at 1.84:1 is genuinely hard to read.
- **Recommended fix:** in dark mode bump these one step lighter — `dark:text-gray-500` → `dark:text-gray-400`, and bare `text-gray-500` sub-labels → add `dark:text-gray-400`. Re-run the Playwright audit to confirm ≥4.5 (large text ≥3.0). The borderline 4.16 cases (14px sub-headers) are a judgment call — bumping them too helps but isn't strictly required for AA.
- **Status:** ⚠ **PARTIALLY FIXED v4.4.26 + DESIGN DECISION NEEDED.** Swept the 12 `dark:text-gray-500` → `dark:text-gray-400` (7 files). Verified improvement: the worst offender (10px "Anthropic Console" label) went **1.84 → 3.42**. BUT a residual remains and it's a **design tension, not a clear bug**:
  - ~54 bare `text-gray-500` 12px sub-labels still measure ~3.03 in dark mode (grok-web descriptors, "1 req", "100%", "util 0%", etc.).
  - These are *intentionally de-emphasized* tertiary text. Pushing them all to `gray-300`/`gray-400` to hit AA 4.5:1 would flatten the visual hierarchy app-wide (secondary text becomes as prominent as primary).
  - 3.03:1 meets AA for *large* text (≥18.66px bold / ≥24px) but these are 12px, so technically AA-fail for normal text.
  - **Operator decision needed:** (a) accept ~3:1 for de-emphasized tertiary labels (common pragmatic bar for internal admin dark UIs), or (b) do a deliberate hierarchy redesign bumping tertiary text + compensating elsewhere. I recommend (a) for an internal tool — the unreadable 1.84:1 case is fixed; the rest is legible, just below the strict threshold.
- **Regression harness:** `/tmp/contrast_a11y_audit.py` (canvas color resolver — handles Tailwind v4 oklch). Re-runnable any time.

### F-OBS-005 — Missing a11y attributes: icon-only buttons + unlabeled form controls — ⚠ **MEASURED (enhancement)** — 2026-05-28

- **Severity:** enhancement
- **Surfaced:** 2026-05-28 Playwright a11y audit (DOM-level: buttons/links with no accessible name, inputs without a label/aria-label, dialog containers without role).
- **Measured findings:**
  | Location | Issue |
  |---|---|
  | **Shared layout (EVERY page)** | icon-only `<button class="hidden md:flex items-center justify-center p-3 bor...">` with no accessible name — one component, fleet-wide. Single fix benefits all pages. |
  | Settings | toggle `<button class="relative inline-flex h-5 w-9 shrink-0 rounded-full">` (a switch) with no name — needs `role="switch"` + `aria-checked` + `aria-label` |
  | Settings | unlabeled `<select>` + unlabeled `<input>` |
  | Providers, Activity | unlabeled `<select>` filter dropdowns |
  | Routing | icon `<button class="p-1 rounded text-gray-400 ...">` (a row action) with no name |
  | Users | icon `<button>` action with no name |
  | metrics, cluster | **a11y OK** |
- **Recommended fix:** add `aria-label` to the icon-only buttons (the shared-layout one is the highest-value single fix); add `aria-label` or associated `<label htmlFor>` to the filter selects; make the Settings toggle a proper `role="switch"`.
- **Status:** ✅ **mostly FIXED v4.4.26 (2026-05-28).** Verified by re-running the Playwright audit before/after:
  - Sidebar collapse button → `aria-label` → cleared on **all 6 pages** that flagged it (dashboard/metrics/providers/api-keys/activity/cluster now a11y-OK).
  - `Switch` component gained `ariaLabel` prop; DynamicSettingsPanel passes `item.label` → Settings toggles named.
  - `Input` component now associates `<label htmlFor>` ↔ `<input id>` via `useId` (app-wide benefit).
  - Activity (severity + error-class), Providers (sort), UserPreferences (timezone + time-format) selects → `aria-label`.
  - Routing: the shared `CopyButton` gained a default `aria-label="Copy to clipboard"`. Users: Edit/Delete row buttons gained `aria-label={`Edit/Delete ${username}`}`. SettingsPage static switches gained `ariaLabel` via the `boolField` helper.
  - ✅ **FULLY CLOSED v4.4.26** — final Playwright a11y audit: **all 9 pages a11y-OK** (dashboard, metrics, providers, api-keys, activity, settings, cluster, routing, users). Zero remaining no-accessible-name / input-no-label findings.

### BUG-083 — `Query(hours, le=720)` accepts negative values silently — 🟡 **OPEN (LOW)**

- **Severity:** low · **Category:** input validation
- **Area:** `app/api/monitoring.py:204` (`metrics_summary`) and any other endpoint using the same `hours: int = Query(24, le=720)` pattern.
- **Repro:** `GET /api/monitoring/metrics?hours=-1` returns 200 with `providers: []`. Internally `created_at >= datetime('now', '-' || -1 || ' hours')` resolves to "anything created after now+1h" = empty set.
- **Fix:** `hours: int = Query(24, ge=1, le=720)`.
- **Tests:** existing input-validation tests cover the upper bound; add lower bound + zero cases.
- **Status:** OPEN, low-severity hardening.

### F-INFRA-001 — Unit-test conftest hits live production at session-finish — ⚠ **NOTED (test infra)**

- **Severity:** low · **Category:** test infrastructure hardening
- **Area:** `tests/conftest.py::pytest_sessionfinish`
- **Concern:** every `pytest tests/unit/` run finishes by POST'ing to `https://www.voipguru.org/llm-proxy2/api/keys/_purge-test-tombstones` (with `verify=False`). This means:
  1. Unit suite is NOT hermetic — running it in CI without network access fails the "purge" step (silently, because best-effort).
  2. `verify=False` disables HTTPS cert verification — `InsecureRequestWarning` is suppressed at top of conftest but the practice itself is a smell.
  3. Running `pytest -W error` (strict warning mode) fails because of the request's HTTP warnings.
- **Recommended fix:** scope the session-finish purge to integration runs only (move to `tests/integration/conftest.py`), OR make it opt-in via environment variable. Replace `verify=False` with a trusted CA bundle.
- **Status:** noted.

### F-INFRA-002 — Playwright suite: stale assertion + 5 timeout-class failures — ⚠ **NOTED (test infra + possible UX/perf)**

- **Severity:** mixed (low for the stale test; medium-unknown for the timeouts)
- **Surfaced:** 2026-05-27 Playwright run against `https://www.voipguru.org/llm-proxy2` — 6 failed / 66 passed / 1 deselected in 3m45s.
- **Stale test:** `TestLLMProxy2API::test_health_endpoint` asserts `data["version"].startswith("2.")` — that string check was written when the proxy was on v2.x. Live version is 4.4.23. The assertion is misleading, not a product bug.
- **Real timeouts** (need triage):
  - `TestLLMProxy2UI::test_create_api_key_flow` — `wait_for_function` 8s exceeded
  - `TestLLMProxy2UI::test_cluster_page_shows_circuit_breakers` — UI render didn't finish
  - `TestProviderActions::test_activity_page_provider_filter` — UI interaction
  - `TestProviderCapabilityEditUI::test_capability_edit_save_closes_modal` — modal didn't close
  - `TestCacheHeaderLive::test_cache_status_header_present` — `read timeout=60` on `/v1/messages` cache header test
- **Suspected causes (need confirmation):**
  - UI tests: SPA may have grown slower; some 8s timeouts may be too tight
  - Cache-header test: 60s timeout on `/v1/messages` is concerning if it's a real proxy-side hang. Could be the upstream provider, could be the proxy. Needs separate investigation.
- **Recommended fix direction:** update the stale version assertion to `startswith("4.")`. Triage each timeout individually — possibly tighten test data setup OR widen the 8s budget OR investigate the proxy slowness on `/v1/messages` for that specific test path.
- **Status:** noted.

### F-OBS-006 — Async-session tracer is live but has zero stack data because of clean uptime — ℹ️ **TRACKING**

- **Severity:** informational
- **Status:** tracking. The v4.4.22 `_TracedAsyncSession` subclass is active on all 3 nodes; `/cluster/db-pool-trace`'s `async_sessions` list is empty because no session has leaked since the v4.4.23 deploy (~5h uptime). The async-side capture is unverified live; only the unit test confirms the mechanism. Need a real leak to surface — or a longer uptime window.



A targeted QA pass run immediately after the v4.4.0 release ceremony to verify the new dormant M-2..M-5 scaffolding is correct, the M-1 hardened bridge is operating cleanly across the fleet, and the v4.3.x recent surfaces survived the version bump. Methodology: doc/code consistency → live fleet state → dormant-scaffolding integrity (M-2 table populated, M-3 writer firing, M-4 wired but no-op, M-5 endpoint live + bundled UI) → cluster sync → wire-path smoke → 24h activity-log baseline.

**Result**: no critical/high defects introduced by v4.4.0. Two low-severity items + one cleanup candidate filed below.

### BUG-051 — M-3 keepalive→auth_state mapping defaults to `needs_reauth` for `rate_limit`/`billing`/`bad_request`/`unknown` — ✅ **CLOSED 2026-05-20 (v4.4.1)**

- **Severity:** low · **Category:** confirmed defect · latent (only fires when Path A is reactivated)
- **Area:** `app/monitoring/keepalive.py:283-293` (M-3 probe→state writer).
- **Repro:** the live row for `(provider_id=8beb17c4bd11de26, node_id=llm-proxy2-c1conv)` in `provider_node_auth_state` shows `auth_state="needs_reauth"` with `last_error` containing a grok.com 429 (`"Too many requests"`). The bridge isn't actually un-authenticated — it's being throttled.
- **Root cause:** the mapping has 4 branches:
  ```
  classify_error == "auth"                              → "needs_reauth"
  classify_error in ("network","timeout","upstream_5xx") → "bridge_down"
  anything else                                         → "needs_reauth"
  ```
  The `classify_error` function returns 8 classes (`auth`, `billing`, `rate_limit`, `timeout`, `network`, `upstream_5xx`, `bad_request`, `unknown`). Four of those eight (`rate_limit`, `billing`, `bad_request`, `unknown`) fall into the catch-all and get stamped as the operator-actionable `needs_reauth` instead of a transient state.
- **Expected:** a 429 from upstream is a transient rate-limit event, not a re-auth ask. It should map to a transient state (e.g. `bridge_down`) so the routing filter (M-4) un-gates the node automatically when the next probe succeeds.
- **Actual today (no impact):** M-4 is dormant in v4.4.0 (0/18 providers have `node_local_session=True`), so the mis-stamped state never gates routing. The row is informational only.
- **Actual on any Path A retry:** the mis-stamped `needs_reauth` would gate the throttled node from routing semipermanently — operator would need to click [Re-auth] for nothing.
- **Fix:** add explicit branch for `rate_limit` → `bridge_down` (it's transient and self-clears on next successful probe); decide policy for `billing`/`bad_request` (probably `needs_reauth` is correct for those); leave `unknown` as `needs_reauth` (conservative).
- **Status:** **CLOSED v4.4.1 2026-05-20.** Fix shipped in
  `app/monitoring/keepalive.py:283-294`: `rate_limit` joined the
  transient bucket (`bridge_down`). Two regression tests added at
  `tests/unit/test_v44_m3_m4_routing_and_cb.py`
  (`test_bug051_rate_limit_maps_to_bridge_down`,
  `test_bug051_billing_and_bad_request_still_needs_reauth`).
  Policy for `billing`/`bad_request`/`unknown` unchanged by design
  (operator-time signal). Latent — no production impact today
  (M-4 dormant); removes the bug from re-activating on any
  future Path A retry.

### BUG-052 — SQLite WAL high-water of 1.097 GB on www1 — ✅ **CLOSED 2026-05-20 (v4.4.4)**

- **Severity:** low · **Category:** observation · operational
- **Area:** `/app/data/llmproxy.db-wal` on llm-proxy2 www1 (volume-mounted, persists across container restarts).
- **Repro:** `stat /app/data/llmproxy.db-wal` shows 1,097,077,752 bytes (1.022 GiB).
- **Diagnosis:** `wal_checkpoint(PASSIVE)` returned `(0, 800, 800)` — busy=0, log_size=800 pages, checkpointed_pages=800. The WAL has been checkpointed cleanly; the 1GB is high-water-mark file space SQLite is preserving for re-use, not active log content. This is normal SQLite behavior when a past burst expanded the WAL and the file wasn't `TRUNCATE`d.
- **Why it matters:** 1 GB of un-truncated WAL means a past write burst was unusually large. The most plausible source is the v3.x.y → 4.4.0 deploy chain's per-version backfills/migrations + heavy cluster-sync activity, but the burst is not currently reproducible.
- **Root cause traced 2026-05-20:** the 2026-05-13 RMAI 1.04B-token
  amplifier loop drove 27× normal proxy volume (32,142 requests
  vs 1,201 baseline). The WAL grew during that burst and stayed
  at the 1.097 GB high-water through every subsequent container
  restart — SQLite reuses WAL pages in place across PASSIVE
  checkpoints, only TRUNCATE mode reclaims the file.
- **One-shot manual TRUNCATE** during Batch G work (v4.4.1)
  reclaimed the 1.097 GB on www1; v4.4.3 deploy chain naturally
  kept it low.
- **Status:** **CLOSED v4.4.4 2026-05-20.** Fix shipped
  `_wal_checkpoint_truncate()` in `app/monitoring/prune.py`,
  wired LAST in the daily sweep so any future storm-class
  high-water is reclaimed automatically. New `wal_reclaimed_bytes`
  + `wal_busy` fields in the `prune.swept` INFO log line for
  visibility. +5 regression tests at
  `tests/unit/test_v443_bug054_bug055.py`.

### CLEANUP-001 — stale Playwright + version-skew test fixture providers in production DB — ✅ **CLOSED 2026-05-20 (mostly auto-deleted; 1 manual reconcile)**

- **Severity:** low · **Category:** housekeeping
- **Area:** `providers` table in production DB.
- **Findings (revised on re-investigation 2026-05-20):** the QA pass reported 18 rows but did not filter on `deleted_at IS NULL`. The actual state: **on www1, all 8 test fixtures already had `deleted_at` set** (between 00:23 and 03:33 UTC today — within hours of each Playwright/skew run completing). So CLEANUP-001 was *already done* by the test fixtures themselves on www1.
- **Cluster-sync drift discovered during cleanup verification (BUG-053 — see below).** www2 + c1conv both still showed `skew-from-new-41a9d6` as active (`deleted_at = NULL`) even though `last_user_edit_at` matched www1's value. The tombstone never propagated. Manually re-applied with a fresh `last_user_edit_at` on both peers — all 3 nodes converged to `active=10`.
- **Status:** **CLOSED 2026-05-20** for the cleanup outcome (all 3 nodes at 10 active providers, 8 tombstones). The underlying cluster-sync issue tracked as `BUG-053`.

### BUG-053 — cluster sync does not replicate `deleted_at` field tombstones when `last_user_edit_at` is unchanged — ✅ **CLOSED 2026-05-20 (v4.4.2)**

- **Severity:** medium · **Category:** confirmed defect · cluster-sync correctness
- **Area:** `app/cluster/manager.py` (push payload for providers) + `app/cluster/sync_handlers.py` (apply handler).
- **Repro:** observed 2026-05-20 21:18 UTC during CLEANUP-001 verification.
  - Provider `391dc40f03f904c4` (`skew-from-new-41a9d6`) on www1: `deleted_at = '2026-05-20 03:33:40.721163'`, `last_user_edit_at = 1779248020.721197`.
  - Same row on www2: `deleted_at = NULL`, `last_user_edit_at = 1779248020.721197` (**identical timestamp**).
  - Same row on c1conv: same as www2.
  - The tombstone has been set on www1 for ~18 hours but has not propagated.
- **Plausible root causes (need code-side confirmation):**
  - (a) The push-sync payload for providers doesn't include `deleted_at` (provider serializer omits soft-deleted columns).
  - (b) The apply handler does include `deleted_at` but uses LWW on `last_user_edit_at`; equal timestamps tie → no update.
  - (c) Both: the LWW comparison correctly skips equal timestamps, but `deleted_at` isn't in the payload anyway.
- **Why it matters:**
  - **Today:** zero routing impact — the row is `enabled=0` everywhere, so peers don't dispatch to it even though they consider it "active".
  - **If we ever soft-delete a real provider** (e.g. operator decommissions an LLM provider via UI without bumping any other column): the tombstone could fail to propagate, and the peer would happily keep routing to the dead provider.
  - **Cluster-state divergence** is a latent failure mode for the cluster's correctness story; a sync that silently drops a column is the kind of bug that hides for months.
- **Symptom workaround (this case):** manually re-applied the tombstone on peers with a fresh `last_user_edit_at` (UNIX time of the SQL UPDATE). LWW now sees a strictly-newer timestamp and won't roll it back from www1.
- **Fix:** read the provider push-sync serializer to confirm (a) vs (b); add `deleted_at` to the payload if missing; if the apply handler uses strict-`>` LWW, change to `>=` or always-merge for tombstone columns. Add a unit test that pushes a tombstone with equal `last_user_edit_at` and asserts the peer's `deleted_at` updates.
- **Root cause confirmed (post-fix investigation):** hypothesis (b)
  — the v2.8.2 tombstone branch gated on
  ``peer_deleted_at >= local_updated``. When background activity
  on the receiver bumped ``local.updated_at`` past the originator's
  ``deleted_at`` timestamp, the branch short-circuited. The
  general LWW field-update path (the "fall-through" for tied
  ``last_user_edit_at`` + strict-greater on ``updated_at``)
  doesn't include ``deleted_at`` in its column set, so there was
  no second path for the tombstone to propagate.
- **Status:** **CLOSED v4.4.2 2026-05-20.** Fix shipped in
  `app/cluster/sync.py:162-200`: tombstone branch now triggers on
  ``peer_deleted_at and not local_deleted`` unconditionally. 3
  regression tests added at `tests/unit/test_cluster_sync_lww.py`:
  `test_bug053_tombstone_propagates_when_local_updated_at_is_newer`,
  `test_bug053_tombstone_propagates_with_tied_user_edit_at`,
  `test_bug053_local_tombstone_not_overwritten_by_peer_tombstone`.
  Unit suite 2265. The original symptom case was already manually
  reconciled in v4.4.1's session, so the fix is preventive — no
  fleet-wide reconcile needed.

### F-OBS-001 — nginx config has 2 pre-existing warnings + bind-mount inode pinning — ✅ **CLOSED 2026-05-21**

- **Severity:** observation only (informational; not a defect introduced by v4.4.0)
- `nginx -t` emits `the "listen ... http2" directive is deprecated` (lines 56, 110) and `protocol options redefined for 0.0.0.0:443` (line 152). The fix is mechanical (replace `listen 443 ssl http2;` with `listen 443 ssl; / http2 on;`).
- **Attempted fix v4.4.12 session — DEFERRED**: edited the host file `/home/dblagbro/docker/config/nginx/nginx.conf`, but the change didn't take effect on the running nginx container.
- **Root cause discovered**: the nginx container's bind mount is **pinned to an old inode** of the host file. Past atomic edits (via `mv tmp file` — which Edit tool does internally) replaced the host file's inode, leaving the container reading a detached inode. The bind mount target's inode 7,109,103 ≠ the current host file's inode 7,109,539. `docker exec nginx cat /etc/nginx/nginx.conf` returns the PRE-edit content despite the host file showing the post-edit content.
- **Resolution required**: container restart (`sudo docker compose up -d --force-recreate --no-deps nginx`) re-binds to the current host inode. This briefly interrupts all proxy traffic served by nginx (~1-3s); should be coordinated with operator presence.
- **Companion gotcha worth recording**: this inode pinning explains why some past nginx config edits have appeared to "not stick" — the container kept reading the old inode until the next nginx container restart. Future nginx config edits should either use truncate-in-place (`cat > file`) instead of atomic-write, OR be followed by a coordinated nginx container restart.
- **Status:** **CLOSED 2026-05-21.** Applied edit to `/home/dblagbro/docker/config/nginx/nginx.conf` lines 56 + 110 (`listen 443 ssl;` + `http2 on;` directive pair). Pre-validated config in a sidecar nginx container before touching the live one. Recreated `nginx` via `sudo docker compose up -d --force-recreate --no-deps nginx`. Total proxy downtime: ~3-5s during container recreate; both `https://www.voipguru.org/llm-proxy2/health` and `https://c1conversations-avaya-01.avaya.c1cx.com/llm-proxy2/health` returned v4.4.12 healthy=10/10 immediately after. Post-restart `nginx -t` no longer emits any `listen ... http2 deprecated` or `protocol options redefined` warnings.
- **Follow-up (2026-05-21):** also removed the redundant `text/html` from two `sub_filter_types` directives (lines 599 + 662 — paperless-anomaly-detector + voicemail-forwarder location blocks). `text/html` is already in `sub_filter_types`'s default set; listing it explicitly triggered a `duplicate MIME type` warning per directive. Second nginx recreate landed cleanly; **`nginx -t` now emits ZERO warnings** for the first time in this configuration's history (post-F-OBS-001-followup state: `syntax is ok` + `test is successful`, no warnings).

---

## 2026-05-20 — Post-v4.4.2 second QA pass (broader sweep)

A second QA pass run after the v4.4.2 deploy to look beyond the
Batch G fixes (which were verified separately). Methodology:
post-deploy error baseline → cluster-state cross-node consistency
on multiple tables → OAuth + scrape freshness → DB integrity / FK
violations → frontend HTML inspection → schema parity → CB / pool /
memory state. No critical/high defects. Three low-severity items
filed below.

### BUG-054 — Production index.html has Vite scaffold title "frontend" — ✅ **CLOSED 2026-05-20 (v4.4.3)**

- **Severity:** low · **Category:** UX / polish
- **Area:** `frontend/index.html` source → `/app/frontend/dist/index.html` in deployed image
- **Repro:** loading `https://www.voipguru.org/llm-proxy2/` shows browser tab title = **frontend**. Source HTML:
  ```html
  <title>frontend</title>
  ```
- **Expected:** something meaningful like `LLM Proxy v2` or `llm-proxy2 admin`.
- **Fix:** edit `frontend/index.html` `<title>` element + rebuild image. Trivial.
- **Status:** **CLOSED v4.4.3 2026-05-20.** `frontend/index.html` line 7
  now reads `<title>llm-proxy v2</title>`. Source-level regression
  guard at `tests/unit/test_v443_bug054_bug055.py::test_bug054_frontend_html_title_is_not_vite_scaffold`.

### BUG-055 — Cumulative orphan refs in activity_log (438 unknown provider_ids + 7,937 unknown api_key_ids) — ✅ **CLOSED 2026-05-20 (v4.4.3)**

- **Severity:** low · **Category:** data hygiene
- **Area:** `activity_log` table on www1 (likely similar on peers).
- **Repro:** SQLite has no FK enforcement (`PRAGMA foreign_keys` not set). Orphan check:
  ```sql
  SELECT COUNT(*) FROM activity_log
  WHERE provider_id NOT IN (SELECT id FROM providers);  -- 438
  SELECT COUNT(*) FROM activity_log
  WHERE api_key_id NOT IN (SELECT id FROM api_keys);    -- 7,937
  ```
  These reference IDs that have been hard-deleted from `providers` / `api_keys` (soft-delete via `deleted_at` keeps the row, so a value showing up as orphan means the row was physically removed at some point — manual cleanup, DB-restore mismatch, or pre-soft-delete-feature deletes).
- **Why it matters:** historical activity-log queries that JOIN to providers / api_keys silently lose rows. Cost-attribution reports, audit traces, and operator forensic queries can underreport by these amounts. Not blocking, but a long-tail correctness erosion.
- **Plus:** `caller_memory` and `caller_memory_marker` each have 1 row referencing `api_keys.id='smoke-test'` which doesn't exist (the smoke-test fixture). 2 FK violations from a stale test.
- **Fix scope:** (a) one-shot DELETE of orphan activity_log rows older than N days; (b) explicit retention policy for activity_log (currently unbounded; 167k rows over 28d = ~135MB in this table alone); (c) the smoke-test fixtures could be hard-deleted since they're soft-deleted already.
- **Status:** **CLOSED v4.4.3 2026-05-20.** Root cause was the
  tombstone-prune step (which is correct + intentional design)
  leaving dangling FK refs in `activity_log`. Fix shipped
  `_prune_activity_log_orphans()` in `app/monitoring/prune.py`,
  wired into the daily sweep AFTER the tombstone-prune step
  (the orphan-creation source). New `activity_log_orphans` counter
  in `_LAST_SWEEP_RESULT` + the `prune.swept` log line.
  +7 regression tests at
  `tests/unit/test_v443_bug054_bug055.py` (incl end-to-end seed +
  prune + verify, no-op-when-clean, source-level ordering guard).
  **Existing accumulated orphans on www1 will be cleaned on the
  first scheduled sweep ~24h after the v4.4.3 deploy.**
  Activity_log retention itself (item (b)) was already in place —
  info=30d / warning=365d / error=1825d — the orphan accumulation
  was a separate gap.

### F-OBS-002 — Tombstoned-row count drift across cluster nodes (design behavior, not a defect) — ⚠ **NOTED**

- **Severity:** observation
- **Area:** `providers` and `api_keys` tables across the cluster
- **Repro:** total row counts:
  - `providers`: www1=18, www2=13, c1conv=13 (active=10 on all 3 — converged)
  - `api_keys`: www1=58, www2=29, c1conv=29 (active=13 on all 3 — converged)
- **Root cause:** by design at `app/cluster/sync.py:327-328` — when a peer's payload carries a tombstoned row that the local node has never seen, the local node skips materializing it ("no point materializing a deleted row"). This means tombstones from a row's lifetime-on-one-node-only never reach peers. Active counts always converge because the active rows DO get materialized.
- **Implications:** the originating node's `providers` / `api_keys` history is more complete than peers'; admin UI showing tombstoned rows will list different counts per node; audit queries that include tombstones will report different totals. Routing is unaffected.
- **Not filed as a fix candidate** — the alternative (always materializing tombstones) would propagate dead rows forever across the cluster for no functional benefit. Documenting here so a future QA pass doesn't re-discover it as a "bug."

### BUG-056 — Gemini providers don't emit `content_block_start` / `content_block_stop` SSE events in Anthropic streaming — ✅ **CLOSED 2026-05-20 (v4.4.5)**

- **Severity:** medium · **Category:** confirmed defect · wire-protocol translation
- **Surfaced:** 2026-05-20 by `tests/integration/test_compatibility_matrix.py::TestWireFormatPerProvider::test_anthropic_stream_all_providers` during the L1 `--run-real` matrix run.
- **Repro:** stream `/v1/messages` with `stream=true` against any Gemini-backed provider (`C1 Vertex AI / Google AI`, `Google Generative LLM`); collect SSE events; the event-type set is missing both `content_block_start` and `content_block_stop`.
- **Expected:** the Anthropic streaming protocol emits `message_start` → (per content block: `content_block_start` → N× `content_block_delta` → `content_block_stop`) → `message_delta` → `message_stop`. SDK clients use the `_start` / `_stop` events to know when a content block begins/ends (relevant for tool-use blocks, multi-block responses, etc.).
- **Actual:** Gemini-translated streams skip the `_start` / `_stop` events; they emit only `content_block_delta` events. Anthropic SDK clients that wait for `content_block_stop` to finalize a block will hang or misparse.
- **Impact:**
  - Affects 2 of 10 active providers (both Gemini-backed).
  - Anthropic SDK clients streaming through these providers see incomplete frame sequences.
  - OpenAI-format streams from the same providers are likely also affected — `test_openai_stream_all_providers` passed for Gemini but failed for OpenAI ChatGPT (BUG-057), so Gemini's OpenAI-format streaming is probably OK; only the cross-family Anthropic SSE translation has the gap.
- **Likely root cause area:** the proxy's Gemini→Anthropic streaming translator (somewhere in `app/api/messages.py` or a translation helper) emits text deltas but doesn't wrap them in `content_block_start` + `content_block_stop` envelopes.
- **Fix scope:** locate the Gemini-streaming translator; wrap the emitted delta sequence in proper Anthropic SSE event types. Probably ~50-100 LoC + 2-3 streaming-fixture tests.
- **Root cause traced 2026-05-20:** litellm's Gemini integration sometimes
  emits a single chunk with `delta.content=None` and only `finish_reason`
  set (especially when truncating at `max_tokens` before any text is
  generated, or when a short response buffers into the terminator chunk).
  The proxy's `_stream_anthropic` loop never flips `text_started=True`,
  and the post-loop guard `if text_started or tool_started:` short-
  circuits — so no content_block events fire.
- **Status:** **CLOSED v4.4.5 2026-05-20.** Fix shipped at
  `app/api/_messages_streaming.py::_stream_anthropic`: synthetic empty
  text block (`content_block_start` + `content_block_stop` for `text=""`)
  emitted when neither text nor tool content was streamed. Real-content
  path unchanged. +5 regression tests at
  `tests/unit/test_v445_bug056_empty_stream.py` (source-level guards +
  end-to-end with mock litellm stream). Unit suite 2282.

### BUG-057 — OpenAI streaming responses missing `finish_reason` in last chunk — ✅ **CLOSED 2026-05-20 (v4.4.6)**

- **Severity:** medium · **Category:** confirmed defect · wire-protocol completeness
- **Surfaced:** 2026-05-20 by `tests/integration/test_compatibility_matrix.py::TestWireFormatPerProvider::test_openai_stream_all_providers` during the L1 matrix run.
- **Repro:** stream `/v1/chat/completions` with `stream=true` against `Devin Personal OpenAI ChatGPT`; collect SSE chunks; the last chunk's `choices[0].finish_reason` is `null` instead of `"stop"` (or `"length"`, `"tool_calls"`, etc.).
- **Expected:** the OpenAI streaming spec requires the final delta chunk to carry `finish_reason` (`"stop"` for normal end-of-message, `"length"` for max_tokens hit, `"tool_calls"` for tool-use, `"content_filter"` for moderation). Clients use it to detect end-of-stream cleanly.
- **Actual:** the last chunk's `finish_reason` is null / missing. Clients that rely on it to terminate stream loops will block waiting for the `[DONE]` sentinel or time out.
- **Impact:**
  - Affects 1 of 10 active providers (`Devin Personal OpenAI ChatGPT` — the OpenAI ChatGPT subscription-OAuth provider, not the API-key OpenRouter path).
  - Strict OpenAI SDK clients (`openai-python` ≥ 1.0) parse `finish_reason` for completion-state machines — would hang or misreport.
- **Likely root cause area:** the OpenAI ChatGPT OAuth streaming translator in `app/api/_oauth_chat_translate.py` or similar — the upstream subscription endpoint emits a slightly different end-of-stream format than the API-key endpoint, and our translator misses the `finish_reason` field on the synthesized last chunk.
- **Fix scope:** locate the ChatGPT-OAuth stream translator; ensure `finish_reason` is populated on the final chunk before `[DONE]`. Probably ~10-20 LoC + 1 streaming-fixture test.
- **Root cause refined 2026-05-20:** not actually the ChatGPT-OAuth
  path — this provider has `provider_type='openai'` and goes through
  the standard litellm OpenAI streaming. The defect is general
  OpenAI-streaming: modern OpenAI emits a usage chunk AFTER the
  finish_reason chunk (when `stream_options.include_usage=true`,
  which litellm defaults to). The usage chunk has `finish_reason=null`,
  so the LAST emitted chunk lacks the end-of-stream signal.
- **Status:** **CLOSED v4.4.6 2026-05-20.** Fix shipped at
  `app/api/_completions_streaming.py::_stream_openai`: buffer-and-
  patch — track most recent finish_reason across the stream, patch
  the last chunk in place before serializing. Preserves usage info
  AND restores end-of-stream signal. +6 regression tests at
  `tests/unit/test_v446_bug057_openai_finish_reason.py` (source-
  level guards + end-to-end mock streams for the usage-chunk
  pattern, classic stream, and no-finish-anywhere defensive case).
  Unit suite 2288.

### BUG-058 — Matrix test assertions too tight for Gemini's verbose response prefix — ✅ **CLOSED 2026-05-20 (v4.4.8)**

- **Severity:** low · **Category:** test-side defect
- **Surfaced:** 2026-05-20 (L1 matrix run, `test_multi_turn_context` + `test_stream_non_stream_content_equivalent` failing for Gemini providers only).
- **Repro:** `multi_turn_context` asks "Define a Python class named \`Stack\` with push and pop" with `max_tokens=150`. Gemini responds "Okay, here's a Python class named Stack..." — but the test polls the first 200 chars and looks for "Stack" literal. With Gemini's "Okay, here's a..." preamble + the Stack code itself, the literal "Stack" word appears past character 200 in some responses. Similar for `stream_non_stream_content_equivalent` asking "How many letters in 'banana'? Just say the number." with max_tokens=20; Gemini answers "It returns 6" or "It returns the number 6" — the "6" digit lands past token 20 truncation.
- **Expected behavior:** test should accept "Okay, here's..." preambles OR raise max_tokens enough to fit Gemini's verbose style.
- **Fix scope:** either (a) widen the assertion to scan the full response text instead of the first 200 chars, (b) prompt-engineer the question to suppress preambles ("Reply with ONLY the class definition, no preamble"), or (c) raise `max_tokens` from 150→256 and from 20→64 for the stream-consistency test. ~10 LoC + 0 new tests.
- **Status:** **CLOSED v4.4.8 2026-05-20.** Three-pronged fix in
  `tests/integration/test_compatibility_matrix.py`:
  - `test_multi_turn_context`: prompt now includes "Output only the
    code, no preamble or explanation"; `max_tokens` 150 → 256;
    assertion widened to accept push/pop/def as well as Stack/class.
  - `test_stream_non_stream_content_equivalent`: prompt now reads
    "Reply with the digit alone, then a brief sentence"; `max_tokens`
    60 → 100.
  No new unit tests (this is a test-side polish; the existing matrix
  tests validate via `--run-real`). Skipped v4.4.7 per operator
  direction.

### F-OBS-004 — Containers run with unbounded memory + CPU limits — ✅ **CLOSED 2026-05-20 (fleet-wide compose edit)**

- **Severity:** observation (defensive engineering)
- **Area:** `/home/dblagbro/docker/docker-compose.yml` (www1 + www2) + `/opt/C1/instance/docker-compose.yml` (c1conv) — `llm-proxy2` + (www1 only) `llm-proxy2-grok-bridge` service definitions.
- **Pre-fix state:** `HostConfig.Memory = 0`, `HostConfig.NanoCpus = 0` on all 3 nodes' proxy containers + the bridge.
- **Fix applied 2026-05-20:** added `deploy.resources.limits` blocks (compose-spec v3 pattern, matching the convention used by other services in the same compose files like wordpress and Flowise):
  - `llm-proxy2` (all 3 nodes): `limits: { cpus: '4.0', memory: 4G }`, `reservations: { cpus: '1.0', memory: 1G }`.
  - `llm-proxy2-grok-bridge` (www1 only — Path B): `limits: { cpus: '2.0', memory: 2G }`, `reservations: { cpus: '0.5', memory: 512M }`.
- **Verification post-recreate:**
  - www1 proxy: `Memory=4294967296 NanoCpus=4000000000`, 10/10 healthy, RSS 217 MB (5.31% of 4 GB).
  - www1 bridge: `Memory=2147483648 NanoCpus=2000000000`, healthcheck=healthy, `logged_in=True`, 10 cookies retained, RSS 591 MB (28.86% of 2 GB).
  - www2 proxy: `Memory=4294967296 NanoCpus=4000000000`, 10/10 healthy.
  - c1conv proxy: `Memory=4294967296 NanoCpus=4000000000`, 10/10 healthy.
- **Method:** rolling per-node `docker compose up -d --force-recreate --no-deps <name>` — no other containers touched, no volumes destroyed.
- **No code change required** in the `llm-proxy-v2/` repo itself; this was purely an infra-side hardening.

### F-OBS-003 — Caller-memory write-back hasn't activated in 5+ days despite flag ON cluster-wide — ⚠ **NOTED + TELEMETRY ADDED v4.4.15**

- **Severity:** observation
- **Area:** `caller_memory` + `caller_memory_marker` tables
- **State:** `caller_memory_enabled = True`, `caller_memory_active_flush_enabled = True`, `caller_memory_recovery_enabled = True` (in `system_settings`). Per memory `project_backlog_caller_memory_live_watch.md`, DevinGPT flipped its consumer-side `proxy_memory_enabled` ON v2.74.51 on 2026-05-15.
- **Empirical**: `caller_memory` has 1 row (`smoke-test`/`c1`/`hello world` from 2026-05-13), already soft-deleted. No new writes in 5 days. `caller_memory_marker` has 1 row, same smoke-test ref.
- **Likely cause:** writes are gated on the inbound `X-Conversation-Id` header (per `feedback_caller_memory_design_locked.md`). No client is sending the header in production traffic. Either DevinGPT's flip hasn't started emitting the header, or the header is being filtered upstream (nginx / proxy stack).
- **Action:** operator already has this on a watch list (`project_backlog_caller_memory_live_watch.md` — "follow-up = check llm_proxy_memory_operations_total health after a day of traffic"). Re-surfaced here because the watch is now 5 days old with no traffic.
- **Not filed as a fix candidate** — diagnosis needed before deciding if it's a proxy bug, a consumer bug, or expected (header isn't being emitted yet because the consumer's roll-out gate hasn't fired).
- **Telemetry added v4.4.15**: rather than periodically diff the `caller_memory` table, the proxy now records `llm_proxy_conversation_id_requests_total{endpoint, has_conversation_id}` on every `/v1/messages` + `/v1/chat/completions` request, and exposes a glanceable admin endpoint `GET /api/monitoring/conversation-id-stats` with a `header_seen` boolean. **How to resolve F-OBS-003**: watch `header_seen`. While `false`, no consumer is sending `X-Conversation-Id` and the dormancy is expected (consumer-side rollout pending). The instant it flips `true`, caller-memory write-back is live — confirm `caller_memory` row count climbs + `llm_proxy_memory_operations_total{operation="extract"}` increments. If `header_seen=true` but `caller_memory` stays empty, THEN it's a proxy-side bug worth filing.

---

## 2026-05-19 — Post-v4.3.2 verification pass (grok-web findings)

A targeted post-deploy QA after shipping v4.3.2 (the BUG-023 interim noise
patch) surfaced two real defects — one of which is that the v4.3.2 patch
itself is non-functional because its premise was based on a misread of the
grok-web architecture.

### BUG-025 — `llm-proxy2-grok-bridge` on tmrwww01 has a crashed Playwright page — ✅ **MECHANICALLY CLOSED 2026-05-20 (v4.4 M-1)**

- **Severity:** high · **Category:** confirmed defect · operational
- **Area:** `llm-proxy2-grok-bridge` sidecar on tmrwww01.
- **Context:** live fleet, 2026-05-19.
- **Repro:** `docker logs --since 30m llm-proxy2-grok-bridge` shows
  `playwright._impl._errors.Error: Page.goto: Page crashed` on every
  `_capture_statsig_id` attempt. A TCP probe to
  `http://llm-proxy2-grok-bridge:8000/` from inside `llm-proxy2` returns
  `Connection refused` on `/status`, `/health`, and `/` — the FastAPI
  process inside the container isn't accepting connections, even though
  Docker reports the container as `Up 10 days`.
- **Expected:** the bridge responds on port 8000; Playwright's grok.com
  page navigates successfully.
- **Actual:** the bridge's HTTP layer is dead; Playwright's page crashes
  on `goto(grok.com)`. Every grok-web request and keepalive probe through
  the public `bridge_url` (see BUG-023 correction below) fails with
  `error_class=upstream_5xx`.
- **Suspected cause:** Chromium ran out of memory or hit an
  unrecoverable navigation error and the FastAPI wrapper didn't restart
  the page; the container's outer entrypoint is alive but the inner
  service is not (a self-monitoring gap). Possibly correlated with a
  Grok session expiry, but the immediate symptom is a process-level crash.
- **Fix direction:**
  1. **Operational (immediate, low-risk):** `docker restart
     llm-proxy2-grok-bridge` on tmrwww01 — single named container, no
     stack impact. If the bridge persists its Grok cookies it should
     come back logged in; otherwise re-auth.
  2. **Hardening (follow-up):** add a healthcheck to the grok-bridge
     compose service (e.g. `curl /status` every 30 s, restart on
     unhealthy) so a crashed inner service auto-recovers without a
     human noticing manually.
- **Update (2026-05-19 22:38 UTC) — attempted recovery did NOT succeed.**
  Batch A of the consolidated remediation plan was authorised and executed.
  Result: `docker restart` put the container into a crash-loop
  (`exit 3`, `RestartCount: 11`). Root cause of the crash-loop is an
  **image-level startup race** between Xvfb and the FastAPI lifespan —
  Chromium launches before a usable `$DISPLAY` is available:
  ```
  ERROR:ozone_platform_x11.cc(244)] Missing X server or $DISPLAY
  The platform failed to initialize. Exiting.
  ```
  Operator authorised the next step — clear `/data/playwright-state` and
  start fresh. **That also did not help**: the crash recurs with the same
  Xvfb error, confirming the issue is the image's startup orchestration,
  not the persisted Chromium user-data-dir. The 10-day-old "Up" container
  was the lucky win of this race on its original boot; subsequent restarts
  lose the race. The cleared `playwright-state` was tarball-backed-up to
  `/tmp/grok-bridge-playwright-state-bak-20260519T183844Z.tar.gz` (263 MB)
  before deletion, available for forensic re-mount if needed.
- **Current state (2026-05-19):** container **stopped** to halt the
  crash-loop and the log spam. grok-web stays unavailable (same end-user
  outcome as the original silent-zombie state; cleaner from
  observability). Rest of the fleet is unaffected — all 3 nodes serving
  v4.3.2, 9/10 providers each (Grok-Web-Devin CB tripped fleet-wide via
  cluster sync; everything else healthy).
- **Revised fix direction:** the symptom is image-level, not
  operational. The operator's restart-to-recover assumption (and the
  remediation plan's Batch A) was incorrect — the bridge image carries
  a latent startup-race bug exposed only on a fresh container exit. Two
  honest paths forward:
  1. **Patch the grok-bridge image** — fix `start.sh` /
     `supervisord` so Xvfb is fully ready (and `DISPLAY` propagated to
     the FastAPI process) before the lifespan launches Chromium. A
     real image change → rebuild → tag → push → recreate. Smallish but
     needs grok-bridge source access.
  2. **Defer to v4.4** — the per-node-auth architectural arc is going
     to redesign this whole layer anyway (and may switch from a
     persistent-context browser to a fresh-context-per-request model
     that sidesteps Xvfb entirely). Accept that grok-web is down in
     the meantime; grok-web is a tertiary fallback and the rest of
     the proxy is fully healthy. **Recommended.**
- **Original status: DEFERRED to the v4.4 arc** (operator-decided 2026-05-19).
  The expectation was that the broader v4.4 redesign would land before
  the bridge image's startup race got patched. That deferral has been
  superseded by the v4.4 **M-1 image-hardening milestone** which lands
  the same root-cause fix without the larger v4.4 commitment.
- **Resolution (2026-05-20, v4.4 M-1)**: image hardening shipped:
  1. `grok_bridge/Dockerfile` — adds `x11-utils` (for `xdpyinfo`).
  2. `grok_bridge/start.sh` — Xvfb readiness now probed with an actual
     X11 query (`xdpyinfo -display :99`) instead of a socket-file
     existence check. The race that produced `Missing X server or
     $DISPLAY` (the socket file appeared before Xvfb finished init →
     Chromium connection raced into the half-open server) is
     mechanically prevented. Wait window bumped 6s → 30s + finer
     polling + diagnostic dump on timeout.
  3. `docker-compose.yml` — `healthcheck` block on the bridge service
     probes `:8443/healthz` every 30s with `start_period: 60s`. The
     `docker ps "Up"` status now reflects the **inner FastAPI**, not
     just supervisord. This catches the BUG-025-class hidden-failure
     pattern within one health interval.
- **Verification on tmrwww01 (live 2026-05-20)**:
  - Bridge container recreated cleanly with the new image.
  - Startup log: `Xvfb display :99 responsive after 20ds` (2s), then
    `playwright ready; bridge listening` — **no `Missing X server`
    error**.
  - Healthcheck: `starting → healthy` after first 30s probe.
  - `docker inspect` shows `restart_count=0`, `health=healthy`.
  - Proxy fleet `/health` reports `healthyProviders=10/10`; grok-web
    CB `8beb17c4bd11de26` is `closed/failures=0` — provider
    effectively back in routing across the cluster (CB state syncs).
- **Path A vs Path B (v4.4) still applies** for the per-node auth
  + cross-node re-auth UX work, but is no longer crisis-driven —
  M-1 took grok-web from "disabled fleet-wide" to "working again on
  the existing shared-bridge topology." The empirical Grok
  multi-session spike can run on the operator's own schedule.
- **Forensic tarball** at `/tmp/grok-bridge-playwright-state-bak-20260519T183844Z.tar.gz`
  is no longer load-bearing — the bridge regenerated its own state.
  Operator may delete at leisure.

### BUG-026 — v4.3.2 prober-skip patch is non-functional (wrong premise) — ✅ **LIVE v4.3.4 (2026-05-19)**

- **Severity:** medium · **Category:** confirmed defect (regression in
  the v4.3.2 release) · also a test coverage gap
- **Area:** `app/monitoring/keepalive.py` — the `_local_sidecar_reachable`
  short-circuit added for BUG-023.
- **Context:** v4.3.2, live on the fleet.
- **Repro (c1conv, 2026-05-19 post-deploy):**
  - `docker logs llm-proxy2 | grep "no local grok-bridge"` → **0
    matches** since the v4.3.2 recreate. The INFO line the patch logs on
    first detection has never fired.
  - The activity log on c1conv shows new `keepalive_probe` rows for
    Grok-Web-Devin **with `origin_node=llm-proxy2-c1conv`** at the normal
    ~5-minute cadence — the prober is *not* skipping; it's still
    probing and still failing.
- **Root cause:** the patch checks `_local_sidecar_reachable(bridge_url)`
  expecting `bridge_url` to be a docker-internal hostname (e.g.
  `http://llm-proxy2-grok-bridge:8000`). The actual `bridge_url` in the
  provider config is the **public URL** (hostname `www.voipguru.org`) —
  one shared bridge on tmrwww01, all 3 nodes reach it through public
  nginx. A reachability HTTP GET to the public URL always succeeds (TLS
  connect + nginx responds), so the check returns `True` and the skip
  branch is never taken. The grok-web architecture is *shared bridge via
  public URL*, **not** per-node sidecars — invalidating the entire
  premise of the v4.3.2 fix.
- **Expected:** the patch suppresses grok-web probes / noise on nodes
  where the bridge is unreachable.
- **Actual:** the patch is a no-op in production. The noise on c1conv
  (BUG-023's symptom) was never the absence of a local sidecar — it was
  upstream-5xx errors from the (now-crashed, see BUG-025) shared bridge.
- **Suspected cause:** I diagnosed BUG-023 by inspecting the c1conv
  containers (seeing no `grok-bridge`) and inferring "missing local
  sidecar" — without verifying that the provider config's `bridge_url`
  was docker-internal. It isn't.
- **Fix direction:**
  1. **Revert the v4.3.2 keepalive.py change** (it's dead code in
     production and adds noise to the codebase). The interim noise
     suppression goal will be obsolete once BUG-025 is fixed (a working
     bridge stops the errors at source).
  2. **OR** keep the helper (`_local_sidecar_reachable` is useful as a
     general primitive) but make the *gate* condition correct — detect
     a docker-internal vs public bridge URL, or skip only on explicit
     `ConnectError` from the actual probe attempt rather than from a
     speculative pre-check.
  3. **Add a unit/integration test** that exercises the skip path with a
     real public URL (or stub) so a future patch can't accidentally
     no-op the way this one did.
- **Resolution (2026-05-19):** v4.3.4 takes option 1 (revert).
  Removed: `_no_local_sidecar` set, `is_no_local_sidecar()`,
  `_local_sidecar_reachable()`, the v4.3.2 gate branch inside
  `_probe_one()`'s grok-web arm, and `tests/unit/test_v432_no_local_sidecar.py`
  (3 tests). Unit suite drops 2148 → 2145; all green. No callers of
  `is_no_local_sidecar()` existed outside the deleted test file, so the
  revert is local in every sense.

  The compose-level grok-bridge healthcheck mentioned in the Batch B
  plan is deliberately NOT included — the bridge container is stopped
  (BUG-025 deferred to v4.4) and the v4.4 redesign will reshape what a
  "healthcheck" should look like for the v4.4 architecture. Adding a
  watchdog around a known-bad startup race now would be churn.
  ✅ **LIVE on all 3 nodes 2026-05-19 (v4.3.4)** — fleet on `version: 4.3.4`,
  `status: healthy`, 10/10 providers each.

### BUG-023 — diagnosis corrected (re-opened, but underlying issue is BUG-025)

The earlier diagnosis ("c1conv lacks the grok-bridge sidecar") was
**incorrect**. grok-web is a *shared* bridge architecture: one
`llm-proxy2-grok-bridge` container on tmrwww01, all nodes reach it via
the public URL `bridge_url=https://www.voipguru.org/...`. c1conv was
never expected to have a local bridge — the noise it produced was
the bridge's own upstream errors hitting the prober. With BUG-025
(bridge crashed) addressed, BUG-023's symptom resolves naturally; the
v4.3.2 work was barking up the wrong tree.

---

## 2026-05-19 (later) — F3 compat-matrix + nginx-restart findings

### BUG-043 — `OpenRouter-Devin-Personal` returns HTTP 400 on standard request — ✅ **FIXED 2026-05-19** (test-side; was bad input)

- **Discovered:** 2026-05-19 by `test_compatibility_matrix.py --run-real`
  (BUG-035 execution). Every one of the 12 matrix wire-format tests
  failed against this provider with HTTP 400 — but `activity_log` has
  NO error rows for the provider in the test window.
- **Hypothesis:** the proxy's pre-routing validation rejects the
  matrix's request shape before reaching the upstream — possibly the
  matrix sends a model name (`default_model`) that doesn't match any
  capability/alias on this provider, and the 400 comes from the
  capability filter rather than from OpenRouter.
- **Repro:** `curl -X POST https://www.voipguru.org/llm-proxy2/v1/messages
  -H "x-api-key: $KEY" -H "Content-Type: application/json"
  -d '{"model":"<default_model>","max_tokens":20,"messages":[{"role":"user","content":"Say OK"}]}'`
  using OpenRouter-Devin-Personal's configured `default_model`.
- **Fix direction:** one diagnostic session — capture the 400's `detail`,
  trace it back to the proxy code path that rejected it, decide whether
  the validation is correct (then update the matrix test's request) or
  wrong (then fix the validator).
- **Severity:** medium. Likely also affects any external caller that
  sends this exact shape — but no production caller has reported it,
  so impact is bounded.
- **Resolution (2026-05-19):** root cause was **bad test input**, not
  a proxy defect. Diagnostic curl confirmed the 400 body:
  `"messages: 'model' field is required and must be a non-empty
  string."` — i.e. the proxy's pre-routing validator correctly
  rejected the empty model name. The provider's `default_model`
  field is the empty string, and the matrix test passed it through
  raw. The proxy's behaviour is correct (and is the same v3.5.8
  validator that closes BUG-004/005). Fix lives in the test: new
  `_pick_chat_model(admin_session, provider)` helper in
  `tests/integration/test_compatibility_matrix.py` resolves a
  chat-capable model via `default_model` → scanned capabilities →
  per-provider-type fallback. 24 unit tests in
  `tests/unit/test_compat_matrix_chat_model_picker.py` pin each
  branch + caching. Live re-run: provider no longer in the 400
  failure list.

### BUG-044 — `Devin-Cohere` returns HTTP 400 on standard request — ✅ **FIXED 2026-05-19** (test-side; was bad input)

Same shape and provenance as BUG-043, against the Cohere provider.
Cohere has historically required different request fields than the
OpenAI-shape default; the matrix test's generic shape may not match.

**Resolution (2026-05-19):** root cause was the provider's
`default_model` set to `embed-english-v3.0` (a Cohere embedding
model). The proxy's pre-routing validator correctly rejected:
`"Model 'embed-english-v3.0' is an embeddings model. Use POST
/v1/embeddings instead of /v1/messages."` Fix lives in the
matrix test (same `_pick_chat_model` helper as BUG-043). Live
re-run: provider no longer in the 400 failure list.

### BUG-045 — `C1 Anthropic Claude` returns HTTP 400 on standard request — ✅ **400 FIXED 2026-05-19** (test-side); ⚠ **503 EXPOSES CONFIG GAP**

Same shape and provenance as BUG-043. This one is most suspicious
because the matrix tests an Anthropic-wire-format request against an
Anthropic-backed provider — the failure suggests something specific
to the C1 Anthropic provider's config (alias map? model name?) is
rejecting otherwise-valid requests.

**Diagnosis (2026-05-19):** root cause was the provider's
`default_model` set to **null**. The proxy's validator returned the
same `"'model' field is required and must be a non-empty string"`
400 as BUG-043. The `_pick_chat_model` helper now resolves
`claude-haiku-4-5` for this provider (via the anthropic-type
fallback table, because the provider has **zero scanned
`model_capabilities` rows**).

**400 closed but 503 remains** — with a valid model name now passing
the validator, the matrix re-run shows HTTP 503 instead. Cause:
because C1 Anthropic Claude has 0 capability rows, the proxy's
capability-based router doesn't consider it as a route candidate
for `claude-haiku-4-5`. The matrix test force-opens the CBs on the
*other* anthropic providers as it cycles, so by the time it tries
to exercise C1 Anthropic Claude, every anthropic-capable route is
CB-tripped and the request returns 503.

**The 503 is the correct proxy behaviour** given the config. The
remaining issue is an operator-time **config gap** — C1 Anthropic
Claude was never scanned for model capabilities. Click "Scan
Models" on this provider in the admin UI (or POST
`/api/providers/{id}/model-capabilities/infer`) to populate the
capability rows, then the matrix will route through it cleanly.

Filed separately as **CONFIG-001 (operator action)** in the
project notes — not a code bug.

### BUG-046 — nginx restart loop when `llm-proxy2-grok-bridge` upstream is stopped — ✅ **FIXED 2026-05-19**

- **Discovered:** 2026-05-19 during the BUG-035 matrix test window.
  Watched the test run for ~7 min, then API calls started returning
  ConnectionRefusedError. Investigation: nginx had restarted **7 times**
  in the same window, each cycle failing at startup with `[emerg] host
  not found in upstream "llm-proxy2-grok-bridge" in nginx.conf:1041`.
- **Root cause:** the 3 grok-bridge `proxy_pass` directives used literal
  hostnames, which nginx resolves at config-parse time. With the bridge
  container stopped (BUG-025 deferred to v4.4), every nginx reload /
  restart hits an unresolvable upstream and aborts. The vhost is then
  serving on the last successful master config (briefly) until docker
  policy restarts the container — and each restart re-attempts parse
  and fails again. A trigger that reloads nginx (cert renewal hook,
  config-watcher service, signal from elsewhere) makes the bug fire.
- **Severity:** high (any nginx reload event would break the entire
  vhost — not just llm-proxy2 routes, every project sharing this nginx).
- **Fix:** convert the 3 `proxy_pass` directives to **variable-based**
  form (`set $grok_bridge_host llm-proxy2-grok-bridge;` +
  `proxy_pass http://$grok_bridge_host:8443/...`). This defers DNS
  resolution to *request* time via the existing `resolver 127.0.0.11`
  directive (already in scope in the surrounding server block). When
  grok-bridge is stopped, the routes return 502 on request — but
  nginx itself starts cleanly. The third location (`/grok-bridge/`)
  also gains a `rewrite ^/grok-bridge(/.*)$ $1 break;` to preserve
  the prefix-stripping behaviour that literal-form `proxy_pass`
  would have done automatically.
- **Verification:**
  - `nginx -t` clean.
  - `nginx -s reload` clean.
  - nginx container restart count returned to 0 after recreate.
  - All llm-proxy2 routes still 200; grok-bridge auth-gated routes
    still 302 (the new config still serves them when the bridge is up).
- **Edit location:** `/home/dblagbro/docker/config/nginx/nginx.conf`
  (NOT in the llm-proxy-v2 repo). Backup at
  `/home/dblagbro/docker/config/nginx/nginx.conf.bak-pre-bug046-20260520`.

### BUG-047 — Anthropic→OpenAI/Cohere tool-def translation gap — ✅ **LIVE v4.3.8 (2026-05-20)**

- **Discovered:** 2026-05-20 by the proactive-monitoring sweep
  (`docs/proactive-sweep-2026-05-20.md` Finding 3).
- **Symptom:** identical upstream 400s on two different non-anthropic
  providers receiving requests with Anthropic-shape tool definitions:
  Cohere returned `"invalid tool at tools[0]: missing required field:
  'type'"`; OpenAI returned `"Missing required parameter:
  'tools[0].type'."`. Both providers had ~6-7 errors/24h in steady
  state — small but persistent.
- **Root cause:** the cross-family translation gate at
  `app/api/messages.py:259-263` fires on `cross_family_fallback OR
  _has_tool_blocks OR has_images`. None catches a **first-turn**
  request with Anthropic-shape tool DEFINITIONS in `body.tools` but
  no `tool_use`/`tool_result` blocks in messages yet — so the raw
  Anthropic-shape tools reached litellm untranslated and 400'd
  upstream on the missing `type: "function"` envelope.
- **Severity:** medium-to-high. Any caller sending Anthropic-shape
  tool defs to a non-anthropic provider got a 400 instead of a
  tool-call response — a real proxy translation hole.
- **Fix (v4.3.8 staged 2026-05-20):** new helper
  `has_anthropic_tool_defs(tools)` in `app/routing/tool_content.py`
  detects Anthropic-shape entries (have `input_schema` OR lack the
  OpenAI `{type:"function", function:{...}}` envelope). Gate widened
  to also fire on this signal. 12 new unit tests; existing
  v3.10.0-translation-gate-wiring test updated to assert each clause
  as a substring instead of a literal one-line form.
- **Operational impact post-deploy:** Devin-Cohere + Devin Personal
  OpenAI ChatGPT bad_request rates expected to drop to ~0 on tool-
  using requests; activity log will confirm.

### BUG-048 — `error_class=unknown` for Grok-Web bridge errors (classifier coverage gap) — ✅ **FIXED v4.3.9 (staged 2026-05-20)**

- **Discovered:** 2026-05-20 proactive-sweep Finding 4. 47 errors in
  24h classified as `unknown` — all are Grok-Web-Devin bridge
  failures with nested-JSON shape `grok-web bridge XXX: grok.com YYY:
  {...}` that the circuit-breaker classifier's regex doesn't match.
- **Severity:** low — classifier behaviour (CB still trips correctly);
  affects operator dashboard grouping but not routing decisions.
- **Recommended fix:** Pre-strip the "grok-web bridge XXX:" prefix
  in `app/routing/circuit_breaker.py:classify_error()`, then
  re-classify the inner error using the existing regexes. ~10 lines.
- **Not picked up this session** — low priority + grok-bridge is
  stopped pending BUG-025/v4.4 anyway, so the 47 errors will
  naturally drop once v4.4 redesigns the bridge layer.
- **Resolution (2026-05-20, v4.3.9):** root-cause inspection of the
  actual "unknown" error strings revealed two missing patterns:
  1. **`grok-web bridge 404: ...'Conversation' with ID 'X' was not
     found`** (~80% of unknowns) — stale operator-configured Grok
     conversation ID. The bridge keeps trying a conversation that
     was deleted upstream. Fix: added 4xx codes 404/405/409/410/
     413/415/422 + the phrase "not found" to `_BAD_REQUEST_PATTERNS`.
  2. **`grok-web bridge unreachable: Server disconnected without
     sending a response`** — formatted httpx `RemoteProtocolError`
     prose. The existing classifier had the exception NAME
     (`remoteprotocolerror`) but the bridge wrapper surfaces the
     formatted message instead. Fix: added "server disconnected",
     "without sending a response", "bridge unreachable" to
     `_NETWORK_PATTERNS`.
  24 new unit tests in `tests/unit/test_v439_classifier_grok_bridge_coverage.py`
  pin both prod-observed strings + regression tests for existing
  classifications (auth wins over 404, 429 stays rate_limit,
  401/403 stay auth, etc.). Unit suite: 2241 passed (was 2217).

### CONFIG-001 — Operator action items surfaced during the 2026-05-20 sweep — **WITHDRAWN 2026-05-20**

Original entry filed 3 operator items. Operator clarified 2026-05-20:

1. ~~**Devin-Codex-Gmail OAuth scope insufficient**~~ — WAI fixture
   (intentional negative-test). Errors are the success signal that
   auth-failure detection works. See
   `reference_intentional_failing_provider_fixtures.md`. Do not
   flag.

2. ~~**C1 Anthropic Claude API key invalid + 0 model_capabilities**~~ —
   WAI fixture (same pattern as #1). The `"invalid x-api-key"`
   from Anthropic + the 0 scanned capabilities are the success
   signal. Do not propose re-auth, Scan Models, or any fix.

3. **system_settings rows with literal "None" string** — also
   withdrawn as actionable: fixed in v4.3.7 (now LIVE in v4.3.8);
   `_coerce()` converts legacy "None" strings to Python None on
   load. No cleanup query needed.

**Net: CONFIG-001 has zero outstanding items.** Withdrawn entirely.

---

## 2026-05-19 — F2 coverage-pass findings (real validation gaps)

While implementing Sub-batch F2 of the coverage-gaps inventory (the
broader UI + form-validation Playwright pass), two real validation
defects surfaced. Both are persisted to the DB via the live API, so
they are server-side validation gaps, not UI-only issues.

### BUG-041 — `/api/keys` accepts negative `rate_limit_rpm` — ✅ **LIVE v4.3.3 (2026-05-19)**

- **Discovered:** 2026-05-19 by `TestFormValidationNegatives::
  test_create_api_key_rejects_malformed_rate_limit` (F2 pass).
- **Area:** `app/api/apikeys.py` — request validator.
- **Repro:** POST `/api/keys` with `{"name": "x", "rate_limit_rpm": -5}`
  → 200 OK, key is created with `rate_limit_rpm: -5` persisted as-is.
  Expected: 422 / 400 rejecting the negative value (rate limits are
  count-per-minute, non-negative by definition).
- **Severity:** medium. A negative RPM is meaningless to the rate
  limiter; behavior is undefined (silently treated as unlimited, or
  blocks every request, depending on which side of the comparison the
  signed int lands on). No live caller is affected today (the form's
  HTML5 `type='number'` allows -5 through but in normal use operators
  enter positives), but the API should reject the bad input at the
  boundary.
- **Fix direction:** add `ge=0` (or `gt=0`) to the Pydantic model field
  for `rate_limit_rpm`; mirror for `rate_limit_tier` if numeric, and
  for spending caps. One-line schema change + a unit test.
- **Test:** `TestFormValidationNegatives::
  test_create_api_key_rejects_malformed_rate_limit` is currently
  `xfail(strict=False)` documenting the bug — when the fix lands,
  remove the decorator and the test becomes a regression guard.
- **Resolution (2026-05-19):** v4.3.3 adds `Field(default=None, ge=0)`
  to every numeric cap/limit field on `KeyCreate` in
  `app/api/apikeys.py`. `KeyUpdate`'s documented "-1 to clear"
  sentinel is preserved (PATCH path unchanged). 15 unit tests in
  `tests/unit/test_v433_create_validation.py` cover both fix +
  preserved semantics. F2 Playwright xfail decorator removed.
  ✅ **LIVE on all 3 nodes 2026-05-19 (v4.3.3)** — regression test passes against the deployed fleet.

### BUG-042 — `/api/users` accepts empty password — ✅ **LIVE v4.3.3 (2026-05-19)**

- **Discovered:** 2026-05-19 by `TestFormValidationNegatives::
  test_create_user_form_rejects_empty_password` (F2 pass).
- **Area:** `app/api/admin.py` (or wherever the user-create endpoint
  lives) — request validator.
- **Repro:** Add User modal — fill username only, click Create. A user
  named `pw-validation-<uuid>` is persisted with an empty / hashable-
  empty password (post-cleanup confirmed via `DELETE /api/users/<id>`
  returning 200, so the row was real). The frontend's HTML5 `required`
  on the password input did not block submission in a non-interactive
  fill-and-submit flow.
- **Severity:** medium. An empty-password user is an authentication
  hole: depending on how `bcrypt.checkpw(b"", hashed_empty)` resolves
  in this codebase, the account may be unauthenticatable (annoying but
  not a security issue) or authenticatable with any input (severe).
  Either way, the row should never have been accepted.
- **Fix direction:** add a non-empty validator on the password field
  in the user-create Pydantic model; require minimum length (e.g. 8).
  Frontend's HTML5 `required` should remain (defense in depth) but the
  server-side check is the load-bearing one.
- **Test:** `TestFormValidationNegatives::
  test_create_user_form_rejects_empty_password` is currently
  `xfail(strict=False)` documenting the bug — when the fix lands,
  remove the decorator and the test becomes a regression guard.
- **Resolution (2026-05-19):** v4.3.3 adds `Field(..., min_length=8)`
  to `UserCreate.password` and `Field(..., min_length=1)` to
  `UserCreate.username` in `app/api/users.py`. `UserUpdate`'s
  "empty password = no change" semantic is preserved (PATCH path
  unchanged — the route-level `if body.password:` check still
  governs partial updates). 15 unit tests cover both fix + preserved
  semantics; F2 Playwright xfail decorator removed.
  ✅ **LIVE on all 3 nodes 2026-05-19 (v4.3.3)** — regression test passes against the deployed fleet.

---

## 2026-05-19 — Coverage gaps inventory (audit of v4.3.0 + v4.3.2 QA scope)

Operator-requested audit of what *was not tested* during the v4.3.0 deep
QA pass and the v4.3.2 post-deploy verification, so the bug queue
formally captures every bounded-out surface. These are **coverage
findings**, not defects — no failure has been observed because no test
has been run; they exist to make the gap visible in a future QA pass and
to inform v4.4 / Batch C scoping.

Each is recorded as a `BUG-NNN` for queue uniformity, with
**Category: test coverage gap** (or **observability/doc gap** where
applicable) and **Severity: low** unless noted.

### BUG-027 — Broader admin-UI pages not deep-tested — **CLOSED 2026-05-19 (F2)**

- **Area:** Providers (add / edit / delete / capability edit), API Keys
  (create / revoke / rate limits / spending caps), Users (manage / RBAC),
  Settings panel (full surface), Activity Log (filters / sort /
  pagination), Metrics, Cluster status page.
- **What's missing:** the v4.3.0 deep QA focused on the AIRI / Routing
  surface + proxy core sanity. Full UI flows for these pages
  (validation, persistence-on-reload, modal/dialog states, empty/error
  states) were not exercised.
- **Fix direction:** add a Playwright pass per page (smoke-level: render,
  one happy CRUD per resource, one negative validation, console-error
  check). Best done as a single follow-up pass before the next deep
  regression cycle.
- **Resolution (2026-05-19, F2):** three new Playwright classes added to
  `tests/integration/test_playwright_ui.py`:
  `TestActivityLogFilters` (search submit + severity filter + clear-all),
  `TestMetricsPageRender` (render + window selector), and
  `TestSettingsPagePersistence` (render + a save/reload round-trip on
  `circuit_breaker_threshold`). All green against the live deployment.

### BUG-028 — Form-validation depth beyond `/api/airi/speak` + auth — **CLOSED 2026-05-19 (F2); surfaced BUG-041 + BUG-042**

- **Area:** all create/edit forms (Providers, API Keys, Rules, Scheduled
  Rules, Notification Prefs, Settings).
- **What's missing:** empty / malformed / oversized / unsupported-value
  inputs are only systematically tested for `/api/airi/speak` and the
  auth endpoints; the other forms rely on their own (untested) Pydantic
  validators.
- **Fix direction:** a small fuzz-table per form (empty, max-length,
  special chars, wrong types) at the API layer; one negative-validation
  Playwright case per form.
- **Resolution (2026-05-19, F2):** `TestFormValidationNegatives` added
  with two cases (empty password on user create; negative
  `rate_limit_rpm` on key create). Both pass the input through and
  surfaced **real API validation gaps** — see BUG-041 + BUG-042 above.
  Tests are currently `xfail(strict=False)` and will convert to
  regression guards when those underlying bugs are fixed.

### BUG-029 — Data persistence + reload depth — **CLOSED 2026-05-19 (F2)**

- **Area:** Routing / AIRI panels (only the AIRI sticky-chat reload was
  covered); Settings; Provider edits; Rule-set activation; API Key
  rate-limit + spending caps.
- **What's missing:** "edit → save → reload page → confirm persisted"
  flow for the surfaces above. Cluster-sync angle ("save on tmrwww01 →
  appears on tmrwww02") also not exercised for non-AIRI surfaces.
- **Fix direction:** one Playwright save-reload pair per editable
  surface; one cluster-sync verification per surface known to sync.
- **Resolution (2026-05-19, F2):**
  `TestSettingsPagePersistence::test_circuit_breaker_threshold_round_trips_through_reload`
  (settings save+reload) and `TestProviderPersistence::test_created_provider_survives_reload`
  (provider create+reload+cleanup) added. Both green. Cluster-sync
  per-surface verification remains a future deeper pass.

### BUG-030 — Cache behavior not live-exercised — **CLOSED 2026-05-19 (F2)**

- **Area:** request cache (`app/api/_cache_inject.py`, the cache
  decision in `_request_pipeline.py`).
- **What's missing:** live verification that cache hits return the
  prior response, cache writes happen on miss, and cache eviction /
  invalidation work end-to-end. Unit tests (`tests/unit/test_cache_inject.py`)
  cover the helper logic; live integration was not exercised this pass.
- **Fix direction:** add a `tests/integration/test_cache_live.py` that
  drives one repeat-request pair through the live proxy and asserts the
  second is a cache hit (header `LLM-Cache: hit`).
- **Resolution (2026-05-19, F2):** `TestCacheHeaderLive::test_cache_status_header_present`
  added — issues two identical `/v1/messages` calls and asserts the
  `X-Cache-Status` header is present on both (proves the cache decision
  is wired into the response pipeline). Header name corrected from the
  fix-direction note's `LLM-Cache` to the actual `X-Cache-Status`
  (set in `app/api/_request_pipeline.py:432-438`).

### BUG-031 — Notifications dispatch not live-tested — ✅ **LIVE v4.3.6 (2026-05-19)**

- **Area:** AIRI rule-fire email path (`app/airi/notify.py` +
  `notify_prefs.py`) — the v4.0.3 surface.
- **What's missing:** the code is source-greped only; a real notification
  dispatch (SMTP send) hasn't been exercised in QA since v4.0.3 shipped.
- **Fix direction:** stage a no-op monitor rule that fires once, observe
  the email is delivered (or captured by a stubbed SMTP); verify
  preference filtering excludes opted-out recipients.
- **Deferral (2026-05-19, F2):** safe live testing requires a
  `dry_run` / test-mode flag in `app/airi/notify.py` that returns the
  rendered email body without performing an SMTP send. Without that
  flag, a live test would spam the operator's inbox each run. A small
  notifier code change unblocks this — captured as a follow-up; unit
  suite continues to cover rendering + recipient-filter logic.
  Inline TODO marker placed in `tests/integration/test_playwright_ui.py`
  above the `TestResponsiveLayout` class.
- **Resolution (2026-05-19, v4.3.6):** `airi_notify(...)` gains
  `dry_run: bool = False` (also honors `AIRI_NOTIFY_DRY_RUN` env var).
  New admin-only endpoint `POST /api/airi/notify/_test_dispatch` is
  the HTTP front door — body `{subject, message, severity, category}`,
  response is the planned-dispatch dict (subject + body + resolved
  recipients) with NO SMTP send. 14 new unit tests in
  `tests/unit/test_v436_notify_dry_run.py` cover both paths (param +
  env var), production-path regression guard, and the truthy / falsy
  parametrized matrix. ✅ **LIVE on all 3 nodes 2026-05-19 (v4.3.6).**
  Verified end-to-end: `POST /api/airi/notify/_test_dispatch` with a
  test payload returned 200 with the full planned-dispatch dict
  (subject, body with `/routing` deep link, recipients) and the
  operator's inbox saw no email — confirming the dry_run path
  exercises the full notifier without SMTP. Side observation: the
  `recipients` array showed `["None"]` (literal string) rather than
  empty, which means `settings.smtp_to` is currently the literal
  string `"None"` in prod — small separate finding worth follow-up.

### BUG-032 — Mobile / responsive layout not exercised — **CLOSED 2026-05-19 (F2)**

- **Area:** the whole app, but especially the AIRI panel input row
  (3 voice buttons + input + Send) at narrow widths, and the off-canvas
  sidebar from v4.0.1.
- **What's missing:** Playwright viewport-emulation runs (`375x812`
  mobile, `768x1024` tablet) of the main pages. Quick visual contrast +
  no-clip checks.
- **Fix direction:** a small responsive sweep (3 viewports × 5 pages =
  ~15 screenshots) reviewed for clipping / overflow / hidden controls.
- **Resolution (2026-05-19, F2):** `TestResponsiveLayout::test_no_horizontal_overflow`
  parametrized across 2 viewports (375x812 mobile, 768x1024 tablet) ×
  6 main pages (Providers, API Keys, Users, Activity, Metrics,
  Settings) = **12 test cases, all green**. No horizontal overflow on
  any page at either viewport. Sidebar collapses correctly at mobile
  width (off-canvas behavior). Hidden controls / mobile-only nav check
  not part of this pass (would need a hamburger-menu probe).

### BUG-033 — Deep keyboard accessibility not exercised — **CLOSED 2026-05-19 (F2 baseline)**

- **Area:** all interactive surfaces. Baseline a11y is present (real
  `<button>`s + `aria-label` + `aria-pressed` on voice buttons), but
  full keyboard-only flow (Tab order, focus visibility, Enter/Space
  activate, Esc dismiss for modals) wasn't driven.
- **What's missing:** a Playwright keyboard-only walk-through of: login →
  navigate sidebar → open AIRI panel → run a chat → manage a provider.
- **Fix direction:** one keyboard-only Playwright test per main flow;
  also enable `motion-reduce` emulation to confirm BUG-024's guard.
- **Resolution (2026-05-19, F2):** `TestKeyboardAccessibility`
  added — two tests cover the highest-traffic surfaces: login form
  submittable via Tab + Enter, and sidebar nav links are focusable
  (real `<a>` or `<button>`). Both green. A full keyboard-only
  walkthrough per main flow is the next a11y arc; `motion-reduce`
  Playwright emulation also pending.

### BUG-034 — Full integration suite not run end-to-end this pass — **CLOSED 2026-05-19 (F3)**

- **Area:** `tests/integration/` outside `test_playwright_ui.py::TestAiriTTS`.
- **What's missing:** `test_api_keys.py`, `test_auth.py`,
  `test_compatibility_matrix.py`, `test_cross_family_translation.py`,
  `test_manual_override_flow.py`, `test_new_features.py`,
  `test_routing_mock.py`, `test_settings_api.py`,
  `test_settings_permutation.py` were not run this session. The 2133
  unit tests + `TestAiriTTS` alone don't exercise the integration paths
  these cover.
- **Fix direction:** run `python3 -m pytest tests/integration/ -rs
  --timeout=60` and triage; expect BUG-001/002/003 to fire (already
  logged), file new findings against any new failures.
- **Resolution (2026-05-19, F3):** suite run end-to-end twice
  consecutively, ignoring `test_playwright_ui.py` (covered by F2).
  Result: **66 passed / 16 skipped / 0 failed**, both runs. Earlier
  BUG-001/002/003 did not reproduce — BUG-002 cannot reproduce here
  because `pytest-xdist` is not installed; BUG-001 + BUG-003 stayed
  green across both runs. Detailed results in `docs/f3-runbooks.md`.

### BUG-035 — Real-provider compatibility matrix not run — ✅ **RAN 2026-05-19 (F3); surfaced BUG-043 + BUG-044 + BUG-045 + BUG-046**

- **Area:** `tests/integration/test_compatibility_matrix.py --run-real`.
- **What's missing:** the `--run-real` flag spends money on live providers
  and is gated as pre-release in `test-plan.md`. The v4.3.0/v4.3.2
  releases didn't run it.
- **Fix direction:** run it once before the next *minor* release
  (v4.4-ish) to catch upstream-shape changes (especially Anthropic /
  OpenAI / Codex / Grok API surfaces).
- **F3 disposition (2026-05-19):** invocation runbook + cost estimate
  (~$1 / ~5 min runtime) + pre-flight checklist captured in
  `docs/f3-runbooks.md` §"BUG-035". Operator-triggered before the
  next minor release — does not block F3 closure for the rest of the
  inventory.
- **Execution (2026-05-19):** **1 passed / 12 failed / 0 skipped**, ~7.7
  min runtime. Two failure classes surfaced:
  1. **3 providers return HTTP 400** consistently across all 12 wire-
     format tests: OpenRouter-Devin-Personal (BUG-043), Devin-Cohere
     (BUG-044), C1 Anthropic Claude (BUG-045). The proxy's
     activity_log has NO error rows for these providers in the test
     window, which suggests the 400 is coming from the proxy's
     pre-routing validation layer, not the upstream — i.e. the
     request shape the matrix sends is being rejected before
     reaching the provider. Worth one focused diagnostic session.
  2. **Content-truncation failures** on Vertex / Google Generative
     (`max_tokens=20` clips the response before the model can
     emit the keyword the test asserts). This is test-side
     brittleness, not a product defect — file as test-infra cleanup.
  3. **BUG-046 (nginx restart loop)** also surfaced during the test
     window: while the matrix was hammering the proxy, nginx tried
     to reload (probably for cert-renewal or a daemon hook) and the
     reload aborted because `llm-proxy2-grok-bridge` was unresolvable.
     7 restarts in succession before the bridge came back up. Fixed
     same session via variable-based proxy_pass in nginx.conf — see
     BUG-046 entry.

### BUG-036 — Rollback drill never exercised — ✅ **CLOSED 2026-05-19**

- **Area:** `docs/backup-plan.md` procedures.
- **What's missing:** the documented rollback procedures (retag a prior
  image → `compose up -d --force-recreate --no-deps` per node, restore
  per-node compose `.bak-pre-v…`, etc.) have never been executed end-to-
  end. A documented-but-unverified rollback is a hope, not a procedure.
- **Fix direction:** one-shot drill on a throwaway stack — roll forward
  to a candidate version, perform the documented rollback, confirm the
  prior version is fully restored. Record outcomes + actual times in
  `backup-plan.md`.
- **F3 disposition (2026-05-19):** drill runbook captured in
  `docs/f3-runbooks.md` §"BUG-036" — step-by-step bash, three staging-
  environment options (single VM / second container on same host /
  second VM in network), and explicit PASS/FAIL criteria. Closes once
  a drill is run + recorded in `backup-plan.md`. Operator-triggered.
- **Execution (2026-05-19):** ran the drill on a stage container
  (`llm-proxy2-stage`, port 13456, `CLUSTER_ENABLED=false`, tmpfs
  /app/data). Three image cycles `4.3.4 → 4.3.6 → 4.3.4`, ready-times
  12.92 / 13.66 / 12.99 seconds. PASS — rollback target restored
  cleanly. Persistent-data preservation + cluster-sync rejoin are NOT
  exercised by this drill (stage container is isolated); those cases
  are covered by BUG-037's separate controlled-skew runbook. Full
  outcomes captured in `docs/backup-plan.md` §"Rollback drill —
  2026-05-19 (BUG-036 closure)".

### BUG-037 — Mixed-version cluster-sync (skew test) not exercised — ✅ **CLOSED 2026-05-19**

- **Area:** cluster sync paths (`app/cluster/*`, `app/api/cluster.py`,
  the various `*cluster_sync*` test files).
- **What's missing:** every rolling deploy this session ended with the
  fleet uniform on one version. The intermediate window (e.g. tmrwww01
  on `4.3.2`, tmrwww02 still on `4.3.1`) hasn't been intentionally
  held for verification. v4.3 added the new `/api/auth/session`
  endpoint — does a mixed-version cluster degrade cleanly when one
  side lacks that route?
- **Fix direction:** during the next rolling deploy, *hold* the first-
  node-only state for ~10 minutes and exercise cluster-synced surfaces
  (provider config update on the older node, observe on the newer; vice
  versa). Document version-skew tolerance.
- **F3 disposition (2026-05-19):** 5-assertion checklist captured in
  `docs/f3-runbooks.md` §"BUG-037" — exercised during the **next**
  rolling deploy (no separate session required; this rides along with
  whatever the next minor release is). Closes once the next deploy
  records its outcomes in `qa-notes.md`.
- **Execution (2026-05-19):** manufactured a controlled prod-node
  skew by downgrading tmrwww02 to v4.3.5 while tmrwww01 + c1conv
  stayed on v4.3.6. Held the skew ~146 seconds. All 5 assertions
  PASS: sync OLD→NEW in 30 s, sync NEW→OLD in 10 s, new endpoint
  returned 4xx (not 5xx) on the older node, both nodes healthy
  throughout, no skew-correlated error spike. tmrwww02 re-upgraded
  cleanly. Full outcomes in `docs/qa-notes.md` §"Mixed-version
  cluster-sync skew test — 2026-05-19 (BUG-037 closure)".

### BUG-038 — `architecture.md` does not document CB cluster-sync semantics — **CLOSED 2026-05-19 (F1)**

- **Area:** `architecture.md` (and possibly `docs/lmrh-2.0-bidirectional.md`).
- **What's missing:** the fact that **a single node's circuit-breaker
  state syncs to the entire cluster** — so one node's repeated upstream
  failures degrade *every* node's view of that provider — is not
  documented. Operationally observed during BUG-025 (one bridge crash on
  tmrwww01 tripped grok-web's CB on all 3 nodes).
- **Severity:** low (observability / doc gap).
- **Resolution (2026-05-19, F1):** `architecture.md` §"Cluster sync"
  now carries a "What syncs cluster-wide vs what stays node-local"
  table that enumerates CB state as cluster-synced with the
  "9/10 on one node often signals a problem somewhere else" operator
  note attached.

### BUG-039 — `architecture.md` does not document the grok-bridge public-URL hairpin — **CLOSED 2026-05-19 (F1)**

- **Area:** `architecture.md`, the providers/grok-web section.
- **What's missing:** that `grok-web` providers have `bridge_url` set
  to the **public URL** (e.g. `https://www.voipguru.org/...`) and that
  all 3 nodes route grok-web through one shared bridge on tmrwww01,
  hairpin through public nginx, is undocumented. This gap is what led
  me to misread BUG-023 (assumed per-node sidecars, in fact one shared
  bridge). A 30-second `SELECT extra_config FROM providers` would have
  shown the real architecture.
- **Severity:** medium (its absence cost a wasted v4.3.2 release —
  BUG-026).
- **Resolution (2026-05-19, F1):** the §"grok-bridge sidecar"
  "Cross-node reachability" subsection has been replaced with
  "Sidecar topology — there is exactly ONE grok-bridge in the fleet",
  which makes explicit (a) only tmrwww01 runs the container, (b)
  *every* node — including tmrwww01 itself — reaches it via the public
  URL because `providers.extra_config.bridge_url` cluster-syncs, (c)
  the three operational consequences (no per-node auth state, CB sync
  amplification, the "live-config read before sidecar fix" rule that
  would have prevented BUG-026), and (d) a pointer to the v4.4 arc as
  the planned redesign.

### BUG-040 — `architecture.md` does not document `activity_log` row scope — **CLOSED 2026-05-19 (F1)**

- **Area:** `architecture.md`, monitoring / cluster-sync coverage.
- **What's missing:** `activity_log` rows are **per-node** (each row has
  `event_meta.node_id`; rows are NOT cluster-synced). This contrasts
  with CB state (synced) — an inconsistency worth calling out so a
  diagnostician knows where to look. Observed during BUG-026's
  diagnosis (the 5 recent c1conv probe rows all carried
  `origin_node=llm-proxy2-c1conv`, confirming local-only origin).
- **Severity:** low (doc).
- **Resolution (2026-05-19, F1):** the same §"Cluster sync" table
  that closes BUG-038 also calls out `activity_log` rows as
  node-local / NOT synced, with the asymmetry to CB state explained
  (rows are high-volume; sync overhead would dominate).

---

## 2026-05-18 — QA pass v4.3.0 (AIRI text-to-speech surface)

Deep regression + release-hardening pass on v4.3.0. 2130/2130 unit tests +
~42 live checks pass against the released images on an isolated prod-DB copy.
**No critical / high / medium defects.** 5 low / coverage / operational
findings — none release-blocking. Full report: `docs/4.3-qa-report.md`.

### BUG-020 — Pre-login `/api/auth/me` 401 logs a console error every load

- **Severity:** low · **Category:** observability gap
- **Area:** frontend auth bootstrap
- **Context:** any fresh page load, all themes, v4.3.0 (pre-existing — not
  introduced by v4.3).
- **Repro:** open any page logged-out → DevTools console shows
  `Failed to load resource: 401 (Unauthorized)` for `/api/auth/me`.
- **Expected:** a clean console; the boot auth-probe is a normal "am I
  logged in?" check and 401 is its expected negative answer.
- **Actual:** the 401 surfaces as a red console error every load.
- **Evidence:** 1 console error during an otherwise-clean QA UI run; it maps
  exactly to the boot `/api/auth/me` probe.
- **Suspected cause:** the auth-status probe uses a plain `fetch`; a 401 is
  always logged by the browser as a failed resource load.
- **Fix direction:** treat the boot-probe 401 as expected — it already is,
  functionally; the noise just muddies real-error triage. Low priority.
- **Status:** ✅ implemented on the `v2` branch — additive
  `GET /api/auth/session` (always 200) + the frontend boot probe switched to
  it; `/me` keeps its 401 contract. Verified: 0 console errors on a
  logged-out load. Ships in v4.3.1.

### BUG-021 — TTS message→speak wiring has no automated test

- **Severity:** low · **Category:** test coverage gap
- **Area:** `AiriChatPanel` / `AiriSpeaker` (v4.3)
- **Context:** v4.3.0.
- **Repro:** n/a — the only coverage of "a completed assistant message
  triggers `speakerRef.speak()`" is the live throwaway smoke; the unit tests
  in `test_airi_voice.py` are source-grep assertions.
- **Expected:** an automated test exercising the integrated flow.
- **Actual:** none; a regression here would only be caught by manual QA.
- **Fix direction:** add a Playwright integration test (speaker on → chat
  turn → assert `/api/airi/speak` fires).
- **Status:** ✅ implemented on the `v2` branch — `TestAiriTTS` in
  `tests/integration/test_playwright_ui.py` (stubs the chat SSE, asserts a
  completed message fires `/api/airi/speak`). Verified passing. Ships in v4.3.1.

### BUG-022 — Audible TTS playback unverifiable in headless Chromium

- **Severity:** low · **Category:** test coverage gap
- **Area:** `AiriSpeaker` audio playback (v4.3)
- **Context:** headless QA environment.
- **Repro:** headless Chromium has no audio device; `audio.play()` after a
  non-gesture `message` event cannot be confirmed to produce sound.
- **Expected:** verification that the synthesized clip actually plays.
- **Actual:** QA confirmed `/api/airi/speak` fires and returns a valid WAV,
  and the `<audio>` element is fed — but not that audio is audible, nor the
  autoplay-policy edge case (play() triggered outside a user gesture).
- **Fix direction:** add a real-browser manual check to the release
  checklist; consider priming the `<audio>` element inside the speaker-toggle
  click gesture to harden against autoplay rejection.
- **Status:** ✅ addressed on the `v2` branch — `docs/release-checklist.md`
  adds a manual real-browser TTS audible-playback check (and the autoplay
  edge case to watch). The optional audio-priming code change was not taken
  (no autoplay failure observed). Ships in v4.3.1.

### BUG-023 — c1conv reports 9/10 healthy providers

- **Severity:** low · **Category:** operational / observability
- **Area:** fleet — c1conv node
- **Context:** live fleet, observed during v4.3.0 QA.
- **Repro:** `GET https://34.170.189.19/llm-proxy2/health` → `healthyProviders:9`
  (tmrwww01 + tmrwww02 report 10/10).
- **Expected:** 10/10, matching the other nodes.
- **Actual:** the `Grok-Web-Devin` provider (id `8beb17c4bd11de26`, type
  `grok-web`) is down on c1conv — its circuit breaker is half-open with 5
  failures; **285/285 keepalive probes failed in the last 24 h** (every
  ~5 min, `severity=error`).
- **Root cause (diagnosed 2026-05-19):** c1conv has **no `grok-bridge`
  sidecar**. `grok-web` providers are served only via the `grok-bridge`
  browser-automation sidecar; tmrwww01 runs `llm-proxy2-grok-bridge` (and
  its grok-web CB is *closed* — healthy). The provider config is
  cluster-synced, so `Grok-Web-Devin` is enabled on all 3 nodes, but the
  sidecar is per-node infrastructure and was never deployed on c1conv.
  Not v4.3-related.
- **Fix direction (needs an operator decision — options):**
  1. Deploy a `grok-bridge` sidecar on c1conv. Requires a logged-in Grok
     web session (Grok account credentials / interactive login) — outward-
     facing, credential-laden; an operator task.
  2. Accept that c1conv does not serve grok-web (it is a tertiary fallback;
     the CB correctly excludes it). The cost is the 9/10 health figure and
     ~285 failed keepalive probes/day of log noise on c1conv.
  3. Enhancement: have the keepalive prober skip a provider whose required
     sidecar is absent on the local node, so a node without grok-bridge
     does not probe (and trip on) grok-web.
- **Status:** ✅ interim noise patch shipped in **v4.3.2** (2026-05-19) —
  the keepalive prober now pre-checks `bridge_url` reachability and silently
  skips the probe when the local sidecar isn't there, so the 285/day error
  rows on c1conv are gone and `healthyProviders` is back to 10/10. The
  **proper** fix — actually serving grok-web from c1conv via a per-node
  bridge + a guided cross-node auth UI — is the v4.4 arc
  (`docs/4.4-per-node-auth-design.md`, pending).

### BUG-024 — Voice buttons' pulse animation ignores `prefers-reduced-motion`

- **Severity:** enhancement · **Category:** accessibility
- **Area:** `AiriSpeaker` / `AiriMicButton` / `AiriHandsFree`
- **Context:** v4.3.0 (and pre-existing on the v4.2 mic/hands-free buttons).
- **Repro:** the synthesizing/speaking (and recording) states use Tailwind
  `animate-pulse` with no `motion-reduce:` guard.
- **Expected:** respect `prefers-reduced-motion`.
- **Actual:** the pulse animates regardless of the OS reduced-motion setting.
- **Fix direction:** add `motion-reduce:animate-none` to the three voice
  buttons. Minor.
- **Status:** ✅ implemented on the `v2` branch — `motion-reduce:animate-none`
  added alongside `animate-pulse` on `AiriSpeaker`, `AiriMicButton`,
  `AiriHandsFree`. Ships in v4.3.1.

---

## 2026-05-10 — QA pass v3.7.13 / v3.7.14 (v3.7.x surface)

### Remediation plan (priority order)

| # | Item | Severity | Status |
|---|------|----------|--------|
| 1 | BUG-019 — admin lockout deadlock | **CRITICAL** | ✅ FIXED in v3.7.14 |
| 2 | BUG-016 — cluster sync gap (3 new tables) | medium | ✅ FIXED in v3.7.15 (+ tombstone column for blocked_ips DELETE propagation) |
| 3 | BUG-017 — AI rate limiter recursion guard | high | ✅ FIXED in v3.7.15 (X-Internal-Source tag + filter) |
| 4 | BUG-018 — IP block cache invalidation cross-node | medium | ✅ FIXED in v3.7.15 (bundled with BUG-016) |
| 5 | UI Backlog A — claude-oauth legacy usage fields | enhancement | ✅ FIXED in v3.7.14 (collapsed behind disclosure) |
| 6 | UI Backlog B — codex-oauth → ChatGPT-oauth-plan UI label | enhancement | ✅ FIXED in v3.7.14 (label-only) |
| 7 | Full data rename — codex-oauth value → ChatGPT-oauth-plan | enhancement | OPEN — needs operator approval (breaking; v3.8.0) |

### Open scope item: full-value rename of `codex-oauth` provider_type

The v3.7.14 UI label change ("ChatGPT-oauth-plan (codex-oauth)" displayed in the dropdown) satisfies the user-facing intent without breaking changes. A full string-value rename — changing the actual `provider_type` value from `codex-oauth` to `ChatGPT-oauth-plan` everywhere — has these costs:

- 34 source files updated (94 literal occurrences)
- DB migration to UPDATE existing `providers` rows
- Cluster sync coordination: peers must roll concurrently OR accept a brief mismatch window
- External callers (anyone POSTing to `/api/providers` with `provider_type: "codex-oauth"`) will need to update
- Routing-key matches in `app/routing/router.py`, `app/api/messages.py`, `app/api/completions.py`, etc.

**Recommendation**: do NOT ship the full rename without explicit operator approval. The UI label change captures the intent at near-zero risk; the value rename is breaking. If approved, ship as a major-bump (v3.8.0) with:
1. Idempotent migration in `app/models/database.py` that UPDATEs existing rows
2. Dual-accept compatibility window in API endpoints (accept both old and new values for one minor version)
3. Deprecation log on every old-value match so external callers see warnings
4. Coordinated cluster roll (all 3 nodes within ~5 min of each other)

Files touched (highest-impact, sample):
- `app/api/providers.py` (9 occurrences — validation strings)
- `app/api/providers_oauth.py` (11 — OAuth flow branching)
- `app/routing/router.py` (9 — provider-type filters)
- `app/api/_codex_oauth_dispatch.py` (8 — dispatch helpers)
- `frontend/src/pages/ProvidersPage.tsx` (5 — type guards)
- ... plus 29 more

### BUG-019 — Admin lockout deadlock: middleware 403s the only endpoint that can un-block

- **Severity**: **CRITICAL** (operator self-DoS; no in-band recovery path)
- **Area**: `app/middleware/ip_block.py` (v3.7.11 ASGI front-stack)
- **Reproduction**:
  1. POST `/api/admin/blocked-ips` with `{"ip": "<your own egress IP>"}` (deliberate, or via a future "auto-add" rule from BUG-017)
  2. Try to call `DELETE /api/admin/blocked-ips/<that IP>`
- **Expected**: DELETE succeeds (HTTP 200, row removed)
- **Actual** (pre-fix): DELETE returns 403 "Source IP is blocked by administrator." — the IP block middleware runs before the endpoint handler, so the operator cannot use the API to recover. Direct DB access was required.
- **How it surfaced**: while testing BUG-018 cache invalidation, I added my own LAN-egress IP (192.168.18.1) to the block list. The next request — including the DELETE I was about to make to remove it — was 403'd by the middleware. The DELETE handler itself was fine; it just never ran.
- **Fix shipped**: **v3.7.14**. Middleware now bypasses two narrow path prefixes for any caller, blocked or not:
  - `/api/auth/login` (admin can sign in)
  - `/api/admin/blocked-ips` (admin can list / add / DELETE)

  Both endpoints remain `require_admin`-gated, so a blocked attacker still can't use them — they just don't 403 at the middleware layer. +4 unit tests in `tests/unit/test_v3711_ip_block.py`.
- **E2E verified post-deploy**:
  ```
  POST   /api/admin/blocked-ips    add 192.168.18.1   → 200 ok
  GET    /api/providers            (blocked IP test)  → 403 (block active)
  DELETE /api/admin/blocked-ips/192.168.18.1          → 200 ok (the fix)
  blocked_ips: 0 entries
  ```
- **Status**: **FIXED in v3.7.14** (cluster on .14)

### BUG-018 — IP block cache invalidation is single-node (peers wait ≤30s) ✅ FIXED in v3.7.15

- **Severity**: medium (timing window, not data integrity)
- **Area**: `app/middleware/ip_block.py` (`_TTL_SEC = 30.0`)
- **Reproduction**:
  1. POST `/api/admin/blocked-ips` against www01
  2. Immediately verify www01 enforces (403)
  3. Immediately verify www02 — still 200 until its TTL expires
- **Expected (caller intuition)**: cluster-wide block within seconds
- **Actual**: only the receiving node clears its cache eagerly (via `_clear_cache_for_tests` from the admin write path). Peer nodes pick up the new row via cluster sync + their own 30s TTL refresh.
- **Likely cause**: no pub/sub or sync-broadcast on `blocked_ips` writes. Cluster sync handles the row replication; cache invalidation isn't wired into that sync.
- **Recommended fix**: emit a cluster-sync event for `blocked_ips` writes that calls `_clear_cache_for_tests()` on receipt. Low-risk; pattern already exists for other admin writes.
- **Status**: **OPEN** — accept timing window for now (admin writes are rare; 30s peer-stale window is acceptable for v3.7.x)

### BUG-017 — AI rate limiter has no recursion guard for its own LLM calls ✅ FIXED in v3.7.15

- **Severity**: high (cost-amplifier risk; not a runtime crash)
- **Area**: `app/monitoring/ai_rate_limiter.py` (v3.7.10 + v3.7.12)
- **Reproduction**: enable the AI rate limiter; it calls `http://localhost:3000/v1/messages` with a proxy-internal admin key to classify per-key behavior. That request:
  1. Hits `/v1/messages` — picks a provider, dispatches, returns
  2. Is logged in `activity_log` (per v3.6.2 capture)
  3. Will be included in the NEXT AI-rate-limiter sample window for that internal key
- **Expected**: the AI rate limiter's own calls are excluded from the sample, or marked so they can't recursively be the subject of their own classification
- **Actual**: no recursion guard. Each review cycle includes the previous cycle's prompts in the new prompt's sample, slowly amplifying the prompt size and cost.
- **Why we didn't see it explode yet**: the cycle is hourly + the prompts are tiny. But under sustained operation this is an O(n²) cost in stored sample size.
- **Recommended fix**: tag activity_log rows from the AI rate limiter (`event_meta.source = "ai_rate_limiter"`) and exclude them in `compute_stats` / `pick_sample_previews`. Add a recursion-depth header on the outgoing httpx request as belt-and-braces.
- **Status**: **OPEN** (queued for v3.7.15)

### BUG-016 — Three new v3.7.x tables NOT in cluster sync ✅ FIXED in v3.7.15

- **Severity**: medium (multi-node data drift; not a single-node bug)
- **Area**: `app/cluster/sync.py` table allowlist
- **Reproduction**:
  1. Add an entry to `blocked_ips` on www01 via admin API
  2. Query www02 DB directly: `SELECT * FROM blocked_ips` — 0 rows
- **Expected**: cluster-replicated like `Provider`, `ModelCapability`, `LmrhDims`
- **Actual**: the v3.7.x tables that landed quickly all skipped the sync list:
  - `blocked_ips` (v3.7.11)
  - `api_key_ai_review` (v3.7.10)
  - `external_usage_snapshot` (v3.7.0) — partial: `Provider.anthropic_session_captured_at` syncs but the snapshot rows don't
- **Why this matters**:
  - `blocked_ips`: admin blocks an IP on one node; peers don't enforce until their own scrape catches it (n/a at v3.7.x — peers don't scrape, so peers never block)
  - `api_key_ai_review`: review reports are node-local; operator viewing the UI on www02 won't see reviews that ran on www01
  - `external_usage_snapshot`: each node scrapes independently, multiplying provider-side load 2-3x for no incremental data
- **Recommended fix**: add all three to the cluster-sync allowlist with LWW conflict resolution. For `external_usage_snapshot`, additionally elect a single leader to do the scrape and replicate (separate ticket).
- **Status**: **OPEN** (queued for v3.7.15)

---

## 2026-05-09 — Open findings (QA pass v3.5.7)

### BUG-001 — Test isolation failure: `TestVisionStripping::test_text_only_request_passes_through_unchanged` — ✅ FIXED v3.5.11

- **Severity**: medium (test infrastructure, not production)
- **Area**: `tests/integration/test_new_features.py`
- **Environment**: integration suite, full run only
- **Reproduction**:
  1. `python3 -m pytest tests/integration/ -x --timeout=60`
  2. Failure on `TestVisionStripping::test_text_only_request_passes_through_unchanged`
  3. Same test passes when run in isolation: `python3 -m pytest tests/integration/test_new_features.py::TestVisionStripping::test_text_only_request_passes_through_unchanged -v`
- **Expected**: test passes regardless of run-order
- **Actual**: passes alone, fails when prior tests have run in same session
- **Likely cause**: shared mock-LLM-server state OR DB state OR fixture cleanup gap
- **Recommended fix**: `pytest-randomly` to shuffle test order + identify the contaminator; add session-scoped cleanup of mock server lifecycle
- **Status**: **OPEN** — needs root-cause investigation
- **Owner**: TBD

### BUG-002 — 13 integration test errors from "Address already in use" on mock LLM server — ✅ FIXED v3.5.9

- **Severity**: high (blocks running integration suite cleanly)
- **Area**: `tests/integration/conftest.py` + `tests/mock_llm_server.py`
- **Environment**: integration tests, sequential runs
- **Reproduction**:
  1. Run full integration suite
  2. Observe 13 errors with `OSError: [Errno 98] Address already in use` from `socketserver.bind`
- **Expected**: each test gets a clean mock server port or shares a session-scoped one
- **Actual**: tests fight for the same port; later tests fail to bind
- **Likely cause**: mock server fixture not properly tearing down between tests, OR port allocation hardcoded
- **Recommended fix**: use `socket.bind(("", 0))` to grab an OS-assigned port, OR session-scope the mock server fixture, OR add proper teardown
- **Status**: **OPEN**

### BUG-003 — Integration tests pollute the production DB — ✅ FIXED v3.5.11

- **Severity**: high (test contamination of live system)
- **Area**: `tests/integration/conftest.py`
- **Environment**: any integration test run hitting `https://www.voipguru.org/llm-proxy2`
- **Reproduction**:
  1. `python3 -m pytest tests/integration/test_new_features.py`
  2. Query the live DB: `SELECT COUNT(*) FROM providers WHERE name LIKE 'pytest%'`
  3. Find 4+ pytest-mock rows with recent `deleted_at` timestamps
- **Expected**: tests use a sandboxed DB or hard-clean rows after each test
- **Actual**: tests create + soft-delete provider rows in the production DB; orphan circuit-breaker state remains in `_local_states` because `circuit_breaker.py` doesn't clear state when a provider is deleted
- **Likely cause**: tests soft-delete (`deleted_at = now()`) instead of hard-delete; CB state cleanup hook not registered on provider deletion
- **Evidence**: `/health` reports 13 circuit breakers but only 10 providers; the 3 orphans are deleted pytest-mock rows
- **Recommended fix** (two parts):
  1. Test teardown: hard-delete pytest-mock rows from `providers` AND clear CB state for those provider IDs
  2. Production fix: in `app/cluster/sync.py` (or wherever soft-delete propagates), call `circuit_breaker.force_close(provider_id)` + remove from `_local_states` on tombstone propagation
- **Status**: **OPEN**

### BUG-004 — `/v1/chat/completions` accepts requests without `model` field, returns upstream 502 — ✅ FIXED v3.5.8

- **Severity**: medium (poor error UX; can mislead clients into thinking proxy is broken)
- **Area**: `app/api/completions.py` (request validation)
- **Environment**: any caller sending malformed body
- **Reproduction**:
  ```bash
  curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"hi"}]}' \
    https://www.voipguru.org/llm-proxy2/v1/chat/completions
  ```
- **Expected**: HTTP 400 with `{"detail":"model is required"}` (client error)
- **Actual**: HTTP 502 with grok-web bridge 429 leaking through (`{"detail":"grok-web bridge 429: ..."}`)
- **Likely cause**: no front-line schema validation; proxy picks default route (priority 1 = grok-web) and forwards the (broken) body, upstream errors surface as 502
- **Recommended fix**: add a Pydantic `ChatCompletionsRequest` model with `model: str` required, `messages: list = Field(min_length=1)` — let FastAPI return 422 automatically. Add similar validation in `messages.py`.
- **Status**: **OPEN**

### BUG-005 — `/v1/messages` accepts empty POST body, returns 200 with auto-substituted model — ✅ FIXED v3.5.8

- **Severity**: high (silently spends real provider budget on empty client requests; potential DoS amplification)
- **Area**: `app/api/messages.py` (request validation)
- **Reproduction**:
  ```bash
  curl -X POST -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" \
    -H "Content-Type: application/json" -d '{}' \
    https://www.voipguru.org/llm-proxy2/v1/messages
  ```
- **Expected**: HTTP 400 with `{"detail":"model and messages are required"}`
- **Actual**: HTTP 200 with `{"model":"gemini-2.5-flash","content":[{"type":"text","text":"Hello!"}]...}`. Real Vertex AI request was made, real tokens consumed.
- **Likely cause**: no validation that `body.model` is truthy or that `body.messages` is non-empty; proxy treats `{}` as "auto-route, default everything"
- **Recommended fix**: same as BUG-004; require `model` and `messages` (non-empty) at the input layer
- **Severity rationale**: an unauthenticated denial-of-wallet vector — anyone with a leaked API key (with any quota) can issue empty requests and burn provider quota at no cost to themselves. The 401 gate works, but a stolen key is much more dangerous than expected.
- **Status**: **OPEN**

### BUG-006 — Unknown model name silently routes to a default; substitution disclosed only in `LLM-Capability` header — ✅ FIXED v3.5.10

- **Severity**: medium (works as designed, but client SDKs reading `response.model` get wrong value)
- **Area**: `app/routing/router.py` (`cross_family_fallback`)
- **Reproduction**:
  ```bash
  curl -X POST -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
    -d '{"model":"totally-fake-xyz","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}' \
    https://www.voipguru.org/llm-proxy2/v1/messages
  ```
- **Expected**: either HTTP 400 (unknown model) OR HTTP 200 with `response.model = "totally-fake-xyz"` (echoing what was requested) AND a clear `X-Cross-Family-Fallback` header
- **Actual**: HTTP 200 with `response.model = "xai/grok-3"` (or whatever auto-route chose). Disclosure IS present in `LLM-Capability` header (`chosen-because=cross-family-fallback, requested-model=totally-fake-xyz, served-model=xai/grok-3`), but client SDKs that read `response.model` see only the substituted name.
- **Likely cause**: Anthropic SDK's `response.model` consumes the upstream's `model` field, which is the served model, not the requested model
- **Recommended fix**: rewrite the response body's `model` field to the originally-requested name when `cross_family_fallback=True`, OR add an explicit top-level `X-Substituted-From: totally-fake-xyz` response header that's easier for clients to inspect than parsing `LLM-Capability`
- **Status**: **OPEN**

### BUG-007 — Stack-trace leak on invalid `role` value — ✅ FIXED v3.5.8

- **Severity**: high (information disclosure)
- **Area**: `app/api/messages.py` error handling on litellm exceptions
- **Reproduction**:
  ```bash
  curl -X POST -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
    -d '{"model":"x-ai/grok-3","max_tokens":10,"messages":[{"role":"banana","content":"hi"}]}' \
    https://www.voipguru.org/llm-proxy2/v1/messages
  ```
- **Expected**: HTTP 400 with `{"detail":"invalid role: banana"}`
- **Actual**: HTTP 502 with full litellm Python stack trace leaking container paths (`/usr/local/lib/python3.13/site-packages/litellm/...`), file names, line numbers
- **Likely cause**: `litellm.acompletion` raises a typed exception; the proxy catches and returns the exception's `.text` directly without sanitization
- **Recommended fix**: in the exception handler, call `circuit_breaker.classify_error()` on the message; for `bad_request` class, return HTTP 400 with a sanitized message. Never return raw stack traces from upstream SDKs.
- **Status**: **OPEN**

### BUG-008 — Stack-trace leak on negative `max_tokens` — ✅ FIXED v3.5.8

- **Severity**: high (information disclosure)
- **Area**: same as BUG-007
- **Reproduction**:
  ```bash
  curl -X POST -H "x-api-key: $KEY" ... -d '{"model":"x-ai/grok-3","max_tokens":-5,...}'
  ```
- **Expected**: HTTP 400 with `{"detail":"max_tokens must be positive"}`
- **Actual**: HTTP 502 with raw Gemini error response body shown
- **Recommended fix**: same as BUG-007 — sanitize all upstream error returns. Better yet, validate `max_tokens > 0` at the input layer.
- **Status**: **OPEN**

### BUG-009 — SDK `LmrhClient.subscribe()` thread doesn't exit promptly on `stop()` — ✅ FIXED v3.5.9

- **Severity**: medium (graceful-shutdown UX; not a leak)
- **Area**: `sdk/python/lmrh_client.py:_sse_session`
- **Reproduction**:
  ```python
  c = LmrhClient(...)
  t = threading.Thread(target=lambda: c.subscribe(on_snapshot=cb), daemon=True)
  t.start()
  time.sleep(7)
  c.stop()
  t.join(timeout=2.0)
  assert not t.is_alive()  # FAILS
  ```
- **Expected**: thread exits within ~5s of `stop()` (the heartbeat interval default 25s, but ideally much sooner)
- **Actual**: thread blocks inside `for line in resp.iter_lines():` waiting for next event/heartbeat; can take up to `heartbeat_sec` (default 25) to notice the stop signal
- **Likely cause**: `httpx.iter_lines()` doesn't accept a stop event; the for-loop polls between lines but blocks during a line read
- **Recommended fix** (options, in order of effort):
  1. Add `httpx.stream(...)` `timeout=heartbeat_sec * 1.5` so blocked reads time out and the outer loop's `_stop` check fires
  2. Use `httpx.AsyncClient` + asyncio cancellation (more invasive — currently synchronous)
  3. Accept the limitation and document it (heartbeat_sec is the worst-case stop latency)
- **Status**: **OPEN**

### BUG-010 — 3 alias/canonical collisions in `model_capabilities` (cleanup smell) — ✅ FIXED v3.5.10

- **Severity**: low (de-dup logic in /v1/models handles it; no runtime bug)
- **Area**: `app/providers/scanner.py` + leftover pre-v3.4.1 capability rows
- **Reproduction**:
  ```sql
  SELECT * FROM model_capabilities WHERE model_id IN ('grok-3','grok-4','x-ai/grok-3','x-ai/grok-4');
  ```
  Shows BOTH bare-name rows (legacy) AND canonical-name-with-alias rows (v3.4.1).
- **Expected**: bare-name rows cleaned up after the v3.4.1 canonical-only switch; only canonical rows with `aliases=["grok-3"]` should remain
- **Actual**: 3 collisions (bare and prefixed both registered)
- **Recommended fix**: add a v3.5.x maintenance migration that deletes capability rows whose `model_id` appears as an alias on another row's canonical (same provider). Or trigger via "Scan Models" button after operator review.
- **Status**: **OPEN**

### BUG-011 — Cross-cluster ETag drift on `/lmrh/providers` — ✅ FIXED v3.5.10 (documented)

- **Severity**: hardening / documentation gap (NOT a runtime bug, but caller-confusing)
- **Area**: `app/routing/lmrh/snapshot.py` cluster behavior
- **Reproduction**:
  ```bash
  curl -sk -H "Auth: ..." https://www.voipguru.org/llm-proxy2/lmrh/providers -I  # ETag A
  curl -sk -H "Auth: ..." https://www2.voipguru.org/llm-proxy2/lmrh/providers -I  # ETag B
  # A != B even when underlying provider config is identical
  ```
- **Expected (caller intuition)**: ETags match across cluster nodes for the same configuration
- **Actual**: ETags differ because each node aggregates `ProviderMetric` independently (per-node design per architecture.md)
- **Impact**: callers polling via DNS round-robin or a load-balancer see ETag changes that aren't real config changes, defeating the 304-cache optimization
- **Recommended fix** (in priority order):
  1. **Doc fix**: explicitly call out per-node ETag in `docs/lmrh-2.0-bidirectional.md` so callers know to pin to one node OR accept the re-fetch cost
  2. **Optional protocol enhancement**: emit a separate `LMRH-Snapshot-ID` header derived from cluster-replicated config (Provider rows + ModelCapability rows) — would match across nodes and let clients cache cross-node
- **Status**: **OPEN**

### BUG-012 — `/health` returns stale circuit-breaker state for soft-deleted providers — ✅ FIXED v3.5.9

- **Severity**: medium (operator-confusing, no functional impact)
- **Area**: `app/cluster/sync.py` provider-tombstone propagation + `app/routing/circuit_breaker.py` `_local_states` lifecycle
- **Reproduction**:
  - Delete a provider via admin API (soft-delete via `deleted_at`)
  - Wait for cluster sync
  - Check `/health` — circuit breaker state for the deleted provider is still listed
- **Expected**: deleted-provider CB state cleared from `_local_states`
- **Actual**: state persists indefinitely until container restart; `/health` shows ghost CBs
- **Recommended fix**: in the cluster-sync tombstone-propagation handler (and the admin DELETE endpoint), call `circuit_breaker._local_states.pop(provider_id, None)` + `_auth_failed.pop(provider_id, None)`
- **Status**: **OPEN**

---

## Recently fixed (post-QA-pass remediation, v3.5.8 → v3.5.10)

The QA pass on 2026-05-09 found 12 open bugs. **9 of them shipped fixed in v3.5.8 / v3.5.9 / v3.5.10**, leaving only the 3 lowest-severity items (test-isolation flake + integration-DB pollution residual + audit-trail entries below).

### FIXED in v3.5.8 — Input validation + error sanitization

- **BUG-004**: `/v1/chat/completions` accepts requests without `model` field, returns upstream 502
- **BUG-005**: `/v1/messages` accepts empty POST body, returns 200 with auto-substituted model (denial-of-wallet vector)
- **BUG-007**: stack-trace leak on invalid `role` value
- **BUG-008**: stack-trace leak on negative `max_tokens`

All 4 closed by `app/api/_input_validation.py` (NEW): front-line `validate_completion_request()` + `sanitize_upstream_error()`. Both endpoints now return clean HTTP 400 with sanitized messages instead of 200/502 with leaked tracebacks. +19 unit tests in `tests/unit/test_v358_input_validation.py`. Live-verified post-deploy.

### FIXED in v3.5.9 — Test infra + CB cleanup hooks

- **BUG-002**: 13 errors from "Address already in use" on mock LLM server port — `tests/mock_llm_server.py` now defaults to OS-assigned port (`port=0`) and `MockServer.stop()` calls `server_close()` to release the socket immediately
- **BUG-009**: SDK `subscribe()` thread doesn't exit promptly on `stop()` — `_sse_session` now sets `httpx.Timeout(read=heartbeat_sec * 2)` instead of `None`. Live-measured: 8.2s exit vs. previously indefinite
- **BUG-012**: `/health` returns stale circuit-breaker state for soft-deleted providers — `delete_provider` and the cluster-sync tombstone-propagation path now both clear `_local_states` and `_auth_failed`. Live-verified post-deploy: orphan CB count went 13 → 0

### FIXED in v3.5.10 — QA hardening

- **BUG-006**: cross-family substitution disclosure only in `LLM-Capability` header — added `X-Substituted-From` + `X-Substituted-To` response headers (and CORS-exposed them). Browser callers can now detect substitution without parsing RFC 8941 structured-field-values
- **BUG-010**: alias↔canonical collisions in `model_capabilities` — new `tools/cleanup_alias_collisions.py` admin script (idempotent, dry-run support). Shipped inside the Docker image
- **BUG-011**: cross-cluster ETag drift on `/lmrh/providers` — documented in `docs/lmrh-2.0-bidirectional.md` with the load-balancer pinning recommendation

### FIXED in v3.5.11 — Last 2 bugs + second QA sweep

- **BUG-001**: test isolation flake — `mock_ctl` fixture cleared `_received` but never drained `_queue`; an unconsumed leftover from a prior test was served to the next. Added `MockServer.clear_queue()` and called it from the fixture
- **BUG-003**: full-pruge of pytest-mock provider rows on session-end — added `/api/providers/_purge-test-tombstones` admin endpoint mirroring the existing api-keys parallel; `pytest_sessionfinish` now hits both
- **BUG-013** (NEW from second sweep): webhook URL scheme not validated — `X-Webhook-URL: file:///etc/passwd` was accepted by httpx. Now rejects any scheme other than http/https with a 400 (`validate_webhook_url`)
- **BUG-015** (NEW from second sweep): unbounded `stop_sequences` array — 1000-entry payload was silently passed to upstream. Now capped at 16 with a clear 400

## Recently fixed (during today's velocity, pre-QA-pass)

### FIXED — Cache write-back NameError silently swallowed (caught during R1 review)

- v3.5.1: extraction of `maybe_serve_from_cache` initially returned only the response (or None), losing the `cache_decision` local variable that downstream `maybe_store()` calls relied on. The `try: ... except Exception: pass` swallowed the resulting NameError, so cache write-back was quietly broken on every request. Fixed by returning the decision in a tuple.
- Tracked in `docs/refactor-log.md` R1+R2 entry.

### FIXED — `Devin-Anthropic-Max-VG` reporting 256% of weekly limit

- Operator-set `usage_weekly_limit_tokens=20M` was below Anthropic's actual Pro Max allowance. Not strictly a bug (the dashboard correctly surfaced the threshold being crossed), but operator-confusing. v3.5.4 added tooltip clarification that this is an operator-imposed early-warning ceiling, not the actual upstream limit.
