# Release readiness — v4.4.23 deep QA pass (2026-05-27)

Companion to `docs/bug-log.md` 2026-05-27 section, `docs/remediation-plan.md` 2026-05-27 update, and `docs/backup-plan.md` 2026-05-27 addendum.

## Scope

Deep QA pass run **after** v4.4.20 → .21 → .22 → .23 shipped earlier today. Treated v4.4.23 as a release candidate for the cluster-sync + observability arc. Pass classified as: **deep regression + release hardening + integration validation + operational validation + a11y spot-check**. Pre-fix posture per the QA-pass protocol — findings are documented and a remediation path is planned, but **no fixes have been implemented**.

## Coverage executed

| Layer | Activity | Outcome |
|---|---|---|
| **L1 unit** | `pytest tests/unit/` | 2375 passed + 2 skipped — green baseline |
| **L1 unit (strict)** | `pytest -W error` | 6 fail + 1 error — all warning-as-error from deprecated `refresh_access_token` usage + InsecureRequestWarning at sessionfinish (F-INFRA-001) |
| **Cluster contract** | HMAC negative-tests on `/cluster/local-metrics` and `/cluster/sync` | 11/11 pass — auth boundaries enforced correctly |
| **API contract** | Input validation on `?hours=` | found BUG-083 (negatives accepted) |
| **Auth boundaries** | Admin endpoint with no session, bogus bearer, malformed bearer | 3/3 pass — 401 returned correctly |
| **Cross-paradigm auth** | `/cluster/local-metrics` with ADMIN bearer (not HMAC) | correctly rejected with 403 |
| **v4.4.23 header capture** | Per-event `had_x_conversation_id` stamp on `/v1/messages` (with/without/tag-only) | works on www1 + www2 — all 3 cases stamp correctly |
| **v4.4.22 async tracer** | 20-concurrent `/health` + 30-concurrent failed-login | no leaks; ~22ms /health overhead; 1 in-flight session at probe time = expected |
| **v4.4.20 LWW behavioral** | Mint api_key on www1, PATCH `semantic_cache_enabled`, wait 80s, observe peers | **FAILED — key never propagated to peers, surfaced BUG-079 (cluster sync silently broken)** |
| **Schema integrity** | `last_user_edit_at` column existence + stamping on all 3 nodes | column present everywhere; stamping diverges (www1:3 / www2:0 / c1conv:0) confirming BUG-079 |
| **Duplicate-row audit** | `(provider_id, captured_at)` / `(api_key_id, …)` / `(ip)` across 5 candidate tables, all 3 nodes | found 2 duplicates triggering BUG-079; identified pattern affecting 5 of 7 apply handlers (BUG-080) |
| **Sync code audit** | `scalar_one_or_none` in `app/cluster/sync_handlers.py` (all 7 handlers) | 2 of 7 have `.limit(1)` defense; 5 vulnerable (BUG-080) |
| **Sync observability audit** | `push_sync` response handling | no status-code check on outbound POST (BUG-081) |
| **Cron auto-watchers** | F2 verify / DevinGPT header verify / c1conv retry | all 3 armed; F2 watcher CAUGHT BUG-082 LIVE (cache_control not producing upstream hits, 479-req batch sampled) |
| **Playwright** | 73-test UI suite, 1 deselected | 66 passed + 6 failed; 1 stale assertion + 5 timeouts (F-INFRA-002) |
| **Theme/a11y spot** | `text-gray-400` usage in `frontend/src/pages/` + a11y-attribute coverage in `components/ui/` | F-OBS-004 (contrast) + F-OBS-005 (light a11y coverage) |
| **Operational** | bridge state, fleet version, cluster heartbeat, /cluster/status | all healthy on v4.4.23 |
| **Cluster heartbeat vs apply gap** | observed `status=healthy` despite apply_sync 500-ing | misleading-signal pattern documented |

## Findings summary

| ID | Severity | Title | Status |
|---|---|---|---|
| **BUG-079** | **HIGH** | Cluster sync silently broken for ~6d on www2 + c1conv | OPEN — release-blocker for v4.4.24 |
| **BUG-080** | **HIGH** | 5 of 7 apply handlers share the same crash vulnerability | OPEN — fix alongside BUG-079 |
| **BUG-081** | MEDIUM | `push_sync` doesn't inspect peer response status | OPEN — observability gap that masked BUG-079 |
| BUG-082 | MEDIUM (cross-team) | F2 cache_control breakpoint not producing upstream cache hits | OPEN — needs hub-team memo |
| BUG-083 | LOW | `Query(hours)` accepts negative values | OPEN |
| F-OBS-004 | LOW (a11y) | Light-mode `text-gray-400` contrast smell | NOTED |
| F-OBS-005 | enhancement | Light a11y attribute coverage on shared UI | NOTED |
| F-OBS-006 | informational | Async-session tracer unverified live | TRACKING |
| F-INFRA-001 | LOW (test infra) | Unit-test conftest hits live prod at sessionfinish | NOTED |
| F-INFRA-002 | LOW (test infra) + medium-unknown | Playwright suite: 1 stale assertion + 5 timeout-class failures | NOTED — needs per-test triage |

## Release-blocker determination

**v4.4.23 itself is not a release blocker for what it ships** (per-event header capture + 8 unit tests + smoke-validated live).

**BUG-079 is a release-blocker for the v4.4.24 follow-up** that closes the cluster-sync gap. Without v4.4.24, the v4.4.18 + v4.4.20 cluster-sync arc is functionally dead.

## Recommended retest scope after v4.4.24

1. Re-run the live LWW test from this pass — mint key on www1, observe propagation to peers in <70s.
2. Re-run the duplicate-detection probe on all 3 nodes; expect 0 duplicates everywhere.
3. Re-run the cluster contract HMAC negative-tests.
4. Spot-check peer `/cluster/sync` POST returns 200, not 500.
5. Full unit suite — must stay 2375+2 (+ whatever regression tests v4.4.24 adds).

## Files updated by this QA pass

- `docs/bug-log.md` — appended 2026-05-27 section (BUG-079..083, F-OBS-004..006, F-INFRA-001..002)
- `docs/qa-notes.md` — appended 2026-05-27 operational observations
- `docs/remediation-plan.md` — appended 2026-05-27 plan (Priority 0..3 buckets, dependencies)
- `docs/backup-plan.md` — appended 2026-05-27 addendum (snapshot-before-data-fix procedure, rollback)
- `docs/release-readiness-v4.4.23-qa-pass.md` — this file (new)

## Pause point

Per QA-pass protocol: **no fixes implemented**. Operator review needed before:
- Cutting v4.4.24 from the planned scope (BUG-079/080/081 + BUG-083)
- Running the data-fix on www2 + c1conv (irreversible without DB snapshot)
- Drafting the hub-team memo for BUG-082
- Scheduling the hardening PR (F-OBS-004/005 + F-INFRA-001)

