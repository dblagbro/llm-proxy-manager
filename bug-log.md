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
