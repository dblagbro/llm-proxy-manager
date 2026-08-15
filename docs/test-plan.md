# Test plan

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
