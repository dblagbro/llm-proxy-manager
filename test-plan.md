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
