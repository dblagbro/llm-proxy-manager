# Test Plan — llm-proxy-v2

Last refreshed: **2026-06-12** (post-refactor regression sweep, v5.1.0 → v5.3.9).

## Pytest baseline (2026-06-12, v5.3.9)

| Suite | Command | Result |
|---|---|---|
| Unit | `python3 -m pytest tests/unit/` | **2910 / 2910 passing + 2 skipped** (~42s). +240 pins added since 2026-06-05 across v5.2 (vendor-neutrality V1-V3), v5.3.0–v5.3.9 (policy editor, taxonomy endpoint, BoolSystemSetting refactor, openai retry tap, cursor billing parity, cursor-bridge string emulation, Gemini thinking budget clamp, logical alias routing, CB lifecycle hardening). |
| Integration (non-UI) | `pytest tests/integration/ --ignore=tests/integration/test_playwright_ui.py` | not re-run this sweep — env focus was live deep-probe. |
| Integration UI (Playwright) | `pytest tests/integration/test_playwright_ui.py` | not run — no browser in this sweep environment. **Confirmed coverage gap**: no UI smoke for the v5.3.0 ApiKey policy editor; if it shipped DOA we wouldn't know until a customer toggled it. |
| SDK | `pytest sdk/python/` | not re-run this sweep. |

### Critical pins added in this sweep

| Test file | Pins | Catches |
|---|---|---|
| `tests/unit/test_v539_cb_hardening.py` | 10 | CB caller-side classifier, record_outcome wiring (static grep), `_schedule_auto_probe` helper existence, get_state HALF_OPEN transition fires probe, hysteresis pin. |

### Coverage gaps surfaced this sweep — recommended new tests

1. **`test_v540_worker_heartbeat_pin.py`** — assert each long-running worker (keepalive, supervisor, billing scrapes ×3, cluster-sync push, retry-tap) writes a heartbeat row within its declared interval (BUG-069 / BUG-074).
2. **`test_v540_supervisor_runs_and_emits.py`** — assert `supervise_once(force=True)` writes a `provider_review` activity-log row (BUG-070).
3. **`test_v540_compliance_dominant_key_has_policy.py`** — assert every key whose 7-day `compliance_events` count is 0 AND whose `llm_request` traffic share is > 10% has at least one non-NULL policy column (BUG-071).
4. **`test_v540_openai_retry_tap_self_test.py`** — assert the v5.3.4 retry tap captures a synthetic openai retry at boot (BUG-072).
5. **`test_v540_health_surfaces_db_pool.py`** — assert `/health` JSON carries `dbPool: {checked_out, pool_size, oldest_checkout_age_sec, waiters}` (BUG-075).
6. **`test_v540_audit_chain_zero_row_alert.py`** — assert `audit_chain_zero_row_day` warning emits after 3 consecutive zero-row days (BUG-073).
7. **Playwright UI smoke for the v5.3.0 ApiKey policy editor** — assert the four policy fields round-trip (load → edit → save → reload).
8. (Carried) End-to-end Playwright test for ClusterPeersPanel.
9. (Carried) Bridge `_send_via_spa_ui` concurrency unit test with mocked Playwright page.
10. (Carried) Cursor-bridge error-mapping fixture test (BUG-053).
11. (Carried) Cluster-peers integration test.
12. (Carried) Compose-file ambiguity guard (BUG-056).

### High-risk areas added this sweep

- **AI supervisor auto_apply path** (v5.3.9 Tier A) — CB hardening relies on supervisor recovery; supervisor showed zero activity events in 7 days (BUG-070). Until BUG-069/070 land, supervisor failures are silent.
- **Compliance enforcement on dominant key** (BUG-071) — subsystem shipped but not exercised; semantics could quietly drift.
- **Background worker liveness** (BUG-069) — every self-healing loop is observability-blind from the snapshot.

---

## Pytest baseline (2026-06-05, v5.0.21 + hotfixes) — superseded by 2026-06-12 above

| Suite | Command | Result |
|---|---|---|
| Unit | `python3 -m pytest tests/unit/` | **2670 / 2670 passing + 2 skipped** (~42s). +8 new pins in `test_v5021_disable_long_context.py`. Pre-hotfix: 7 failures in `test_v31015_buglog_fixes.py` (BUG-049). |

### Pins from previous sweep

| Test file | Pins | Catches |
|---|---|---|
| `tests/unit/test_v5021_disable_long_context.py` | 8 | BUG-049, BUG-050 regressions |
| `tests/unit/test_v5018_cluster_peer_persistence.py` (pre-existing) | 7 | cluster_peers LWW/tombstone/self-ignore (does NOT pin the frontend path — BUG-051 slipped through). |

---

## Original (stale 2026-05-15) plan continues below


## Validation Scope

| Sweep type | Triggers | Surfaces |
|---|---|---|
| **smoke** | Every deploy | `/health` + version on all 3 nodes, `/v1/messages` happy path, login |
| **standard regression** | Pre-release | full unit suite + non-UI integration + OpenAPI + basic API probes |
| **deep regression** | Major refactors, OAuth changes, release hardening | every surface below + adversarial / negative paths + log inspection + code-level diff audit |

## Pytest baseline (2026-05-15, v3.10.9)

| Suite | Command | Result |
|---|---|---|
| Unit | `python3 -m pytest tests/unit/` | **1973 / 1973 passing** (~27s) — +4 in `test_v31010_buglog_fixes.py` |
| Integration (non-UI) | `pytest tests/integration/ --ignore=tests/integration/test_playwright_ui.py` | post-v3.10.10: `test_revoke_key_rejects_llm_calls` re-pointed to a registered model (was BUG-037 timeout, not BUG-023); `test_release_now_also_enables_v386` (BUG-027) still open |
| Integration UI (Playwright) | `pytest tests/integration/test_playwright_ui.py` | **not run** — no browser in the validation environment (declared coverage gap) |
| SDK | `pytest sdk/python/` | **20 / 20 passing** |
| Cross-family translation | `pytest tests/integration/test_cross_family_translation.py` | 4 / 4 against live www01 |

## Surface Inventory & Validation Method

| # | Surface | Method | Status (2026-05-15) |
|---|---|---|---|
| 1 | `/health`, `/cluster/status` | curl, version regex | ✅ all 3 nodes v3.10.9 healthy |
| 2 | `/openapi.json` | curl + schema | ✅ 125 paths, all have operationId |
| 3 | Admin login | curl, bad-pw/missing-field | ✅ 401 wrong-pw, 422 missing |
| 4 | Session RBAC | non-admin → 403 | ✅ enforced |
| 5 | API-key auth | bogus/missing → 401 | ✅ enforced (BUG-034 wording fixed v3.10.10) |
| 6 | API-key rate limit | rapid hits → 429 | ✅ working |
| 7 | `/v1/messages` non-streaming | live key + provider | ✅ happy path; BUG-025 (malformed-JSON→400) + BUG-030 (GET→JSON-404) fixed v3.10.10; 🛑 BUG-037 unregistered-model hang |
| 8 | `/v1/messages` streaming SSE | curl --no-buffer | ⚠ BUG-001 still open (errors masked as 200) |
| 9 | `/v1/chat/completions` | curl | ✅ happy path; same BUG-025/030 |
| 10 | `/v1/models` | curl | ✅ |
| 11 | `/v1/embeddings` | curl | ✅ 200; ⚠ BUG-035 serializer warnings |
| 12 | Provider CRUD | POST→GET→DELETE→404 | ✅ |
| 13 | `/api/keys/{id}/models` (v3.10.8) | live + RBAC | ✅ correct auth, 404 unknown, no api-key bypass |
| 14 | `/api/providers/_refresh-all-anthropic-billing` (v3.9.19) | live + auth | ✅ POST admin-gated; 🛑 BUG-031 GET→404 not 405 |
| 15 | `/cluster/db-pool-trace` (v3.10.2) | admin auth | ✅ correct auth |
| 16 | `/lmrh/*` v2 endpoints (enabled on www01) | live + auth | ✅ providers/health/config; 🛑 BUG-029 quotes unknown-model; ⚠ BUG-034 |
| 17 | API-key revocation | create→delete→reuse | ✅ revoked key → 401 in 0.0s (BUG-023 auth-bypass claim retracted; `deleted_at` filter hardening shipped v3.10.10) |
| 18 | Settings GET/PUT round-trip | flip + reread | ✅ |
| 19 | Activity log + severity filter | `/api/monitoring/activity` | ✅ (BUG-014 fixed) |
| 20 | Cluster sync | `/cluster/sync` live | ⚠ resurrection risk (BUG-023); two-node E2E untested |
| 21 | claude-oauth dispatch (`_messages_dispatch.py`, v3.10.9) | live smoke + code audit | ✅ smoke 200; ⚠ BUG-024 stale-`extra` fallthrough; ⚠ BUG-036 no behavioral tests |
| 22 | Cross-family translation (v3.10.0) | integration suite | ✅ 4/4; ⚠ BUG-028 two shapes still mishandled |
| 23 | AI provider supervisor (enabled suggest-only, www01) | log + code audit | ✅ runs clean; ⚠ BUG-026 inert recursion guard |
| 24 | Error-rate alert (v3.10.4) | code audit | ✅ decision fn correct; ⚠ blind to ASGI/pool errors (BUG-032) |
| 25 | ARCH-A pool tracer | live | ⚠ leak actively manifesting; tracer off on GCP |
| 26 | Background workers (keepalive, billing scrape, heartbeat) | log inspection | ✅ running clean |
| 27 | Web UI (API Keys / Providers / Metrics / Settings) | `tsc` + code/wiring inspection only | ⚠ **interactive Playwright NOT run — coverage gap** |
| 28 | Logs / observability | live container stderr, 3 nodes | ⚠ BUG-032 ASGI noise; ARCH-A pool errors |

## Coverage Gaps (open)

1. **GAP-1 (HIGH)** — `_messages_dispatch.py` (v3.10.9) has no behavioral test coverage of the dispatch branches → BUG-036.
2. **GAP-2 (HIGH)** — streaming error paths untested for the dispatch extraction; existing SSE-error tests predate it.
3. **GAP-3 (MED)** — ARCH-A pool-leak harness has no leak-reproduction regression test in CI.
4. **GAP-4 (MED)** — cluster sync: no two-node E2E test (write on A, assert on B); BUG-023 is exactly this class.
5. **GAP-5 (MED)** — error-rate alert: pure decision fn tested; the sampler-loop integration (probe exclusion, throttle) is not.
6. **GAP-6 (LOW)** — LMRHv2 Phase 4 (v3.10.6) has no repo test; v3.10.7 `most_reliable` fix has no proxy-side `_provider_hint_match` contract test.
7. **GAP-7 (LOW)** — no interactive Playwright UI run is possible in the current validation environment; UI is inspection-only.
8. Pre-existing gaps still open: no live multi-provider matrix; no load/concurrency soak; no fuzz of `extract_code_from_callback`.

## High-Risk Areas (continuous re-test)

1. `_messages_dispatch.py` claude-oauth chain walk (refactored v3.10.9, ~no behavioral tests).
2. Cluster-sync merge / tombstone resurrection (BUG-023).
3. Streaming error semantics (BUG-001 open).
4. AI supervisor — LLM control loop, suggest-only today (RMAI-incident-class risk if auto-apply flips).
5. ARCH-A pool leak — actively manifesting.
6. Cross-family translation edge shapes (BUG-028).

## Re-test scope after remediation

- After **BUG-023**: re-run `test_revoke_key_rejects_llm_calls`; add a two-node delete→sync convergence test; confirm a revoked key 401s on all 3 nodes within one sync cycle.
- After **BUG-024**: deliberately 401 every claude-oauth provider, confirm the litellm fallthrough provider serves with correct credentials.
- After **BUG-025**: malformed-JSON probe on both `/v1/messages` and `/v1/chat/completions` → expect 400.
- After **BUG-036**: the new `_messages_dispatch` behavioral tests pass; full unit suite green.
- After **ARCH-A**: capture `/cluster/db-pool-trace` during a saturation event; confirm the leaking stack is identified and the fix holds for >24h uptime.
- Every tier: full unit suite + non-UI integration + the cross-family translation integration suite.

---

# Archived: layered test plan (formerly `docs/test-plan.md`)

Merged verbatim on 2026-11-18 from `docs/test-plan.md`, the layer-by-layer plan written
during the v3.5.7 to v4.3 QA passes. Counts, commands and warnings in it are as of those
dates (for example the integration suite no longer targets production by default; see
`docs/testing.md`).

Companion to `docs/architecture.md` + `docs/bug-log.md`. Describes the test surfaces, what kind of testing each gets, and where coverage gaps live.

Created 2026-05-09 during the post-v3.5.7 deep QA pass. Update this doc when adding new endpoints / SDK methods / UI surfaces or when discovering coverage gaps.

---

## Test layers

### Layer 1 — unit tests (`tests/unit/`)

- **Count**: 2288 tests across 100+ files (verified 2026-05-20, v4.4.9; +28 across the v4.4.1-v4.4.9 fix cycle — BUG-051 (×2), BUG-053 (×3), BUG-054/055 (×7), BUG-052 (×5), BUG-056 (×5), BUG-057 (×6))
- **Speed**: full suite runs in ~45s (2288 tests, 2026-05-20 v4.4.9 wall time)
- **Scope**: pure-Python module behavior, no network, no DB except SQLite test DB at `/tmp/llmproxy-unit-test.db`
- **Coverage**: routing, provider scoring, LMRH parse/build, cache decision, CoT pipeline, schema migrations, OAuth flows (mocked), monitoring helpers, model identity (canonical / aliases / family / variant), refactor pass helpers (R1+R2+R3+R4)
- **Strength**: high coverage of pure logic
- **Weakness**: doesn't catch integration-level bugs (auth/DB/network); race conditions and concurrency aren't exercised; mock fixtures may diverge from real upstream behavior

### Layer 2 — SDK tests (`sdk/python/test_*.py`)

- **Count**: 11 tests (LMRH client polling) + 5 tests (subscribe SSE consumer)
- **Speed**: ~5s
- **Scope**: SDK behavior using `httpx.MockTransport` for synthetic HTTP/SSE
- **Coverage**: snapshot dispatch, hint synthesis, polling fallback, ETag round-trip, SSE frame parsing, heartbeat handling, error paths
- **Strength**: real protocol-level testing without network
- **Weakness**: doesn't catch SDK-vs-server protocol drift (when proxy changes wire format); see BUG-009 (subscribe stop-latency)

### Layer 3 — integration tests (`tests/integration/`)

- **Count**: ~30 tests across 7 files
- **Speed**: ~5-10s; some tests skipped without `--run-real` flag
- **Scope**: HTTP requests against the LIVE proxy at `https://www.voipguru.org/llm-proxy2/`. Some tests use a local mock LLM server that binds a fixed port.
- **Coverage**: API key auth, vision stripping, rate limiting, spending caps, settings API, routing decisions
- **Strength**: catches real auth + dispatch bugs; verifies wire shapes
- **Weakness**:
  - **BUG-001**: test isolation issues (test passes alone, fails in full suite)
  - **BUG-002**: mock LLM server port collisions (13 errors in full run)
  - **BUG-003**: integration tests pollute the production DB with `pytest-mock` rows that aren't hard-deleted
  - Missing: no integration tests cover the v3.4.0 SSE stream or v3.5.4 probe-state endpoint
- **Improvement opportunity**: separate "integration-against-prod-fleet" from "integration-against-localhost-mock-stack" — currently mixed

### Layer 4 — Playwright UI tests (`tests/integration/test_playwright_ui.py`)

- **Count**: small set (operator-noted; "each test gets its own browser context")
- **Speed**: slow (~30-60s per test)
- **Scope**: full browser session against the live proxy admin UI
- **Coverage**: login flow, providers page, settings page, dashboard
- **Strength**: catches frontend integration bugs invisible to unit tests
- **Weakness**: not run regularly; requires playwright install + browser. No coverage of the v3.5.x dashboard widgets (Sub Quota stat card, Over-limit banner, Probe Back-off panel)

### Layer 5 — manual / curl-driven smoke (this QA pass)

- **Scope**: negative tests, malformed input, edge cases, multi-node consistency, rate-limit burst, header inspection
- **Findings**: 10 of the 12 open bug entries originated from this layer. **Fastest defect-discovery rate of any layer.**
- **Improvement opportunity**: codify the most valuable manual probes as integration tests (see "Coverage gaps" below).

---

## v3.7.x surfaces — added 2026-05-10

| Surface | Unit | SDK | Integration | UI | Manual | Adequate? |
|---|---|---|---|---|---|---|
| `GET /api/providers/{id}/anthropic-billing-credentials` (v3.7.0) | ✅ | — | ❌ | — | ✅ | partial |
| `POST /api/providers/{id}/anthropic-billing-credentials` cookie paste (v3.7.0) | ✅ | — | ❌ | — | ✅ | partial |
| `POST /api/providers/{id}/anthropic-billing/refresh-now` (v3.7.0) | ✅ | — | ❌ | — | ✅ | partial |
| `GET /api/providers/{id}/anthropic-billing/snapshots` (v3.7.0) | ✅ | — | ❌ | — | ✅ | partial |
| `anthropic_billing_worker` 4-hourly background loop (v3.7.0) | partial | — | ❌ | — | ✅ | **GAP** — no test verifying the loop runs on schedule |
| `external_rotation.evaluate_rules_for_provider` (v3.7.1) | ✅ | — | ❌ | — | ✅ | partial |
| `external_rotation.reorder_claude_oauth_by_utilization` (v3.7.4) | ✅ | — | ❌ | — | ✅ | partial |
| `Provider.auto_skip_until` honored by router (v3.7.1) | ✅ | — | ❌ | — | ✅ | partial |
| Cluster sync of new Provider columns (v3.7.3) | ✅ | — | ❌ | — | ✅ | partial — sync of `anthropic_session_cookies` intentionally skipped |
| `client_ip` + `client_ip_inside` activity-log fields (v3.6.2 / v3.6.3) | ✅ | — | ❌ | — | ✅ | OK after R5 |
| LAN-egress DNS rewrite (v3.6.3) | ✅ | — | ❌ | — | ✅ | OK |
| 412 with fresh ETag on `PUT /api/llm/models/{id}` (v3.6.1) | ✅ | — | ❌ | — | ✅ | OK |
| `AI rate limiter` review pipeline (v3.7.10) | ✅ | — | ❌ | — | ✅ | **GAP** — no recursion-guard test (BUG-017) |
| `AI rate limiter` apply/dismiss/revert API (v3.7.10) | ✅ | — | ❌ | — | ✅ | OK |
| `IP block` middleware basic (v3.7.11) | ✅ | — | ❌ | — | ✅ | OK |
| `IP block` admin-recovery exemption (v3.7.14) | ✅ | — | ❌ | — | ✅ | OK (4 new unit tests + e2e curl) |
| `IP block` cache invalidation cross-node (v3.7.11) | ❌ | — | ❌ | — | ✅ | **GAP** — see BUG-018 |
| `BlockedIp` cluster sync (v3.7.11) | ❌ | — | ❌ | — | ✅ | **GAP** — see BUG-016 |
| AnthropicBillingPanel (v3.7.5) | ❌ | — | ❌ | ❌ | ✅ | **GAP** — no Playwright |
| Edit Provider modal "Usage-based rotation (superseded…)" note (v3.7.5) | ❌ | — | ❌ | ❌ | ✅ | **GAP** — pending Backlog A decision |

### High-priority v3.7.x coverage gaps to close

In priority order (impact × ease-to-test):

1. **Cluster-sync the three new tables** (BUG-016) — add `blocked_ips`, `api_key_ai_review`, `external_usage_snapshot` to the cluster-sync allowlist with LWW conflict resolution. Add unit test asserting each table is enumerated.
2. **Recursion guard on AI rate limiter** (BUG-017) — tag outgoing httpx requests from `review_one_key()` with `event_meta.source = "ai_rate_limiter"` and exclude in `compute_stats`. Unit test for the exclude path.
3. **IP block cache invalidation broadcast** (BUG-018) — emit cluster-sync event on `blocked_ips` write that calls `_clear_cache_for_tests()` on peer nodes. Integration test that POSTs to www01 and verifies www02 enforces within 1s.
4. **AnthropicBillingPanel Playwright** — cover cookie paste → snapshot table render → refresh button → auto-skip banner.
5. **Single-leader external_usage scrape** — when cluster sync of `external_usage_snapshot` lands, also elect a leader so we don't multiply provider load. Separate ticket.

---

## Test surface inventory + coverage status

| Surface | Unit | SDK | Integration | UI | Manual | Adequate? |
|---|---|---|---|---|---|---|
| `/v1/messages` happy path | ✅ | — | partial | — | ✅ | OK |
| `/v1/messages` negative input (empty body, invalid role, negative max_tokens) | partial | — | ❌ | — | ✅ | **GAP** — see BUG-005, BUG-007, BUG-008 |
| `/v1/chat/completions` happy path | ✅ | — | partial | — | ✅ | OK |
| `/v1/chat/completions` negative input (missing model, empty messages) | partial | — | ❌ | — | ✅ | **GAP** — see BUG-004 |
| `/v1/models` (with aliases) | ✅ | — | ❌ | — | ✅ | partial — needs integration test |
| `/lmrh/providers` ETag round-trip | ✅ | ✅ | ❌ | — | ✅ | OK |
| `/lmrh/quotes` validation | ✅ | — | ❌ | — | ✅ | OK |
| `/lmrh/stream` SSE protocol | partial | ✅ | ❌ | — | ✅ | **GAP** — no integration test, no chaos / reconnect coverage |
| `/lmrh/stream` heartbeat behavior | ❌ | partial | ❌ | — | ✅ | **GAP** — no test of the 25s heartbeat actually firing |
| `/.well-known/lmrh-config` | ✅ | partial | ❌ | — | ✅ | OK |
| `/api/monitoring/probe-state` (admin) | ❌ | — | ❌ | — | ✅ | **GAP** — no test of the v3.5.4 endpoint |
| `/api/providers/*` CRUD | ✅ | — | ✅ | — | — | OK |
| Dashboard Sub Quota widget | ❌ | — | ❌ | ❌ | ✅ | **GAP** — no Playwright test for the v3.5.3 widget |
| Dashboard Over-limit banner | ❌ | — | ❌ | ❌ | ✅ | **GAP** |
| Dashboard Probe Back-off panel | ❌ | — | ❌ | ❌ | ✅ | **GAP** for the v3.5.6 widget |
| In-page tooltips (27 added) | ❌ | — | ❌ | partial | ❌ | **GAP** — no automated check that `tooltip` props pass the right strings |
| SDK `LmrhClient.subscribe()` — graceful stop | ❌ | partial | ❌ | — | ✅ | **GAP** — see BUG-009 (no test catches the slow-stop) |
| Cluster sync — provider tombstone CB cleanup | ❌ | — | ❌ | — | ✅ | **GAP** — see BUG-012 |
| Cluster sync — cross-node ETag consistency | ❌ | — | ❌ | — | ✅ | **GAP** — see BUG-011 |
| Cross-family fallback disclosure | ❌ | — | ❌ | — | ✅ | **GAP** — see BUG-006 |
| Bridge `/api/status` (grok-bridge) | ❌ | — | ❌ | — | partial | **GAP** |
| Schema migration idempotency | ❌ | — | ❌ | — | partial (container restart smoke) | **GAP** — no test that runs migrations twice |
| Probe back-off state machine | ✅ | — | ❌ | — | partial | OK on the unit side; integration would need to induce 429s |
| Stack-trace leak prevention | ❌ | — | ❌ | — | ✅ | **GAP** — see BUG-007, BUG-008 |
| Concurrent rate-limit behavior | ❌ | — | ❌ | — | ✅ | partial — burst of 10 confirms rate limit fires |

### High-priority coverage gaps to close

In priority order (impact × ease-to-test):

1. **Front-line input validation** — add Pydantic models for `/v1/messages` and `/v1/chat/completions` request bodies; integration tests for malformed input cases (empty body, missing model, empty messages, negative max_tokens, invalid role). Closes BUG-004, BUG-005, BUG-007, BUG-008.
2. **Stack-trace sanitization** — unit test that asserts upstream litellm exceptions are converted to clean `{"detail": ...}` responses without filenames or line numbers. Closes BUG-007, BUG-008.
3. **SDK subscribe() stop-latency** — pytest-asyncio test that calls `subscribe()` then `stop()` and asserts the thread exits within `heartbeat_sec * 1.5` seconds. Closes BUG-009.
4. **Provider tombstone CB cleanup** — integration test that creates + deletes a provider and asserts `/health` no longer reports CB state. Closes BUG-012.
5. **Dashboard widgets** — Playwright tests for the v3.5.3 + v3.5.6 widgets, checking that the right colors/labels appear when quota over-limit / providers in back-off.
6. **Mock LLM server isolation** — fix the integration test fixture to use OS-assigned ports; add a session-scoped `mock_llm_server` fixture. Closes BUG-002.
7. **Production-DB pollution** — change integration test conftest to use a separate test DB or hard-delete pytest-mock rows in teardown. Closes BUG-003.

---

## How to run

```bash
# Unit + SDK (fast, safe)
cd /mnt/s/code/llm-proxy-v2
rm -f /tmp/llmproxy-unit-test.db
python3 -m pytest tests/unit/ sdk/python/ --ignore=tests/unit/test_runs_cluster.py
# Expect: 1040 passed

# Integration (CAUTION: hits production DB; pollutes provider table)
rm -f /tmp/llmproxy-int.db
python3 -m pytest tests/integration/ -rs --timeout=60
# Known: BUG-001, BUG-002, BUG-003 will fire

# Real-provider test pass (costs $; requires --run-real flag)
python3 -m pytest tests/integration/test_compatibility_matrix.py --run-real

# Playwright (slowest)
python3 -m pytest tests/integration/test_playwright_ui.py -v
```

## Severity / scope conventions

When adding new tests, label severity by:

- **smoke**: 1-3 happy-path checks per surface; runs in pre-deploy hook
- **standard**: full unit + SDK + non-Playwright integration; runs in CI per commit
- **deep regression**: Playwright + real-provider matrix + chaos / load; runs pre-release
- **release hardening**: stack-trace audit, fuzzing, cross-cluster consistency; runs ad-hoc

Today's QA pass was **deep regression / release hardening**. Of the 12 open bugs found, 7 are in coverage gaps that would benefit from a smoke or standard tier test.

---

## v4.2 / v4.3 — AIRI voice surfaces (added 2026-05-18)

### Surfaces

| Surface | What it is | Test method | Coverage |
|---|---|---|---|
| `whisper-bridge` sidecar | self-hosted STT (faster-whisper) + Vosk wake model + TTS (Piper) | direct container tests + via the proxy | good — `/speak` auth + validation all paths; `/transcribe` + `/vosk-model` auth |
| `POST /api/airi/transcribe` | v4.2 STT proxy | API + live smoke | good |
| `POST /api/airi/speak` | v4.3 TTS proxy | API: happy/empty/missing/oversize/malformed/auth | good |
| `GET /api/airi/voice-model` | serves the Vosk model to the browser | API | good |
| push-to-talk mic / hands-free | v4.2 UI | Playwright (capture stubbed — see qa-notes) | moderate |
| speaker toggle (`AiriSpeaker`) | v4.3 UI — reads answers aloud | Playwright: render/state/toggle + integrated flow | moderate |

### Known coverage gaps (v4.3)

- **BUG-021** — the message→speak wiring (`speakerRef.speak()` on the SSE
  `message` event) has no automated test; only the live throwaway smoke.
  A Playwright integration test should be added.
- **BUG-022** — audible TTS output cannot be verified headless (no audio
  device); the autoplay-policy edge case (`play()` outside a user gesture)
  needs a real-browser manual check.
- Mobile/responsive layout of the 3-button voice row not exercised.
- Keyboard-accessibility of the voice buttons not deeply tested (they are
  real `<button>`s with `aria-label` + `aria-pressed`, so baseline a11y is
  present; `prefers-reduced-motion` is not honored — BUG-024).

The v4.3.0 QA pass (2026-05-18) was **deep regression / release hardening**;
full report in `docs/4.3-qa-report.md`.
