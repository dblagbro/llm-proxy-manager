# v4.4.x release-readiness wrap-up

**Run date:** 2026-05-20
**Scope:** the full v4.4.0 → v4.4.9 fix cycle (nine releases in one session; v4.4.7 skipped per operator direction).
**Outcome:** **PASS — zero open defects.** All filed defects from this session closed. Fleet on v4.4.9, all 3 nodes 10/10 healthy.

This document supersedes [`release-readiness-v4.4.0.md`](release-readiness-v4.4.0.md) which is preserved for historical context (it was a per-release artefact at the v4.4.0 cut).

---

## Release chain

| Version | Date | Scope | Tests added | Total tests |
|---|---|---|---|---|
| v4.4.0 | 2026-05-20 | Full v4.4 milestone — bridge image hardening (BUG-025 closed) + Path A scaffolding (M-2..M-5) shipped dormant after empirical rejection | — | 2260 |
| v4.4.1 | 2026-05-20 | BUG-051 (M-3 `rate_limit` → `bridge_down`), BUG-052 operational (one-shot WAL TRUNCATE), CLEANUP-001 (8 test-fixture providers tombstoned cluster-wide) | +2 | 2262 |
| v4.4.2 | 2026-05-20 | BUG-053 (cluster-sync tombstone propagation: `peer_deleted_at >= local_updated` gate replaced with `not local_deleted`) | +3 | 2265 |
| v4.4.3 | 2026-05-20 | BUG-054 (`<title>frontend` → `llm-proxy v2`), BUG-055 (`_prune_activity_log_orphans()` wired into daily sweep + 21,230 orphan cleanup) | +7 | 2272 |
| v4.4.4 | 2026-05-20 | BUG-052 code (`PRAGMA wal_checkpoint(TRUNCATE)` in daily sweep), **L3 pre-cut live-verify** in `tools/cut-release.sh` | +5 | 2277 |
| v4.4.5 | 2026-05-20 | BUG-056 (Anthropic streaming `content_block_start` / `_stop` synthesized when upstream emits no `delta.content`) | +5 | 2282 |
| v4.4.6 | 2026-05-20 | BUG-057 (OpenAI streaming: buffer-and-patch the FINAL chunk's `finish_reason` — modern OpenAI emits usage chunk after finish chunk) | +6 | 2288 |
| **v4.4.7** | — | **SKIPPED** per operator direction | — | — |
| v4.4.8 | 2026-05-20 | BUG-058 (matrix test prompts: no-preamble directive + raised `max_tokens` for Gemini-style verbose responses) — turn 1 fix only | 0 | 2288 |
| v4.4.9 | 2026-05-20 | BUG-058 turn-2 follow-up (multi-turn test's second prompt needed the same fix) | 0 | 2288 |

**Plus operational changes (no version bump):**
- **F-OBS-004 closed** — fleet-wide `deploy.resources.limits` on `llm-proxy2` (4 GB / 4 CPU) + `llm-proxy2-grok-bridge` (2 GB / 2 CPU on www1).
- **L4 closed** — 2 orphan bridge data volumes deleted on tmrwww02 + c1conv (`docker volume rm` with explicit per-volume operator approval).

---

## Defect status at session end

| Category | Count | IDs |
|---|---|---|
| 🔴 Critical / High open | **0** | — |
| 🟡 Medium open | **0** | — |
| 🟢 Low open | **0** | — |
| 🔵 Observations (no fix candidate) | 3 | F-OBS-001 (nginx config — out of repo) · F-OBS-002 (tombstone-row count drift across nodes — design behavior) · F-OBS-003 (caller-memory write-back unwritten 5+ days — consumer-side `X-Conversation-Id` rollout pending) |

---

## Fleet state at session end

| Node | Version | Healthy providers | DB pool | Container limits |
|---|---|---|---|---|
| tmrwww01 (www1) | 4.4.9 | 10/10 | 50 max, 0 in use | 4 GB / 4 CPU (proxy) + 2 GB / 2 CPU (bridge) |
| tmrwww02 (www2) | 4.4.9 | 10/10 | 50 max, 0 in use | 4 GB / 4 CPU |
| c1conv | 4.4.9 | 10/10 | 50 max, 0 in use | 4 GB / 4 CPU |

- Wire path actively serving production traffic.
- All circuit breakers `closed / 0 failures`.
- `provider_node_auth_state` (M-2) cluster-sync propagating correctly across nodes (rows refresh every 5 min via M-3 keepalive probes; visible on all 3 nodes from any peer's query).

---

## Verified working (regression coverage)

| Surface | Verification | Status |
|---|---|---|
| Wire path `/v1/messages` (Anthropic format) | Live traffic + matrix tests `test_anthropic_stream_all_providers` | ✅ |
| Wire path `/v1/chat/completions` (OpenAI format) | Live traffic + matrix tests `test_openai_stream_all_providers` | ✅ |
| Wire path streaming (SSE) | `test_anthropic_stream_all_providers` + `test_openai_stream_all_providers` PASS post-v4.4.6 | ✅ |
| Multi-turn context preservation | `test_multi_turn_context` PASS post-v4.4.9 | ✅ |
| Tool-use across all providers | `test_tool_call_structure_all_providers` PASS (already passing before session) | ✅ |
| Stream vs non-stream content equivalence | `test_stream_non_stream_content_equivalent` PASS post-v4.4.8 | ✅ |
| Cluster sync — provider table | `test_v44_m2_provider_node_auth_state` + post-deploy cross-node verification | ✅ |
| Cluster sync — tombstone propagation | `test_bug053_*` (3 tests) + live reconcile observation | ✅ |
| Anthropic streaming protocol completeness | `test_v445_bug056_empty_stream` (5 tests) + live `--run-real` | ✅ |
| OpenAI streaming end-of-stream signal | `test_v446_bug057_openai_finish_reason` (6 tests) + live `--run-real` | ✅ |
| WAL high-water reclaim | `test_v443_bug054_bug055::test_bug052_*` (5 tests) + first-sweep deferred to ~24h after deploy | ✅ |
| Activity log orphan prune | `test_v443_bug054_bug055::test_bug055_*` (5 tests) + one-shot reclaim verified | ✅ |
| Frontend title | `test_v443_bug054_bug055::test_bug054_*` + live HTML inspection | ✅ |
| Release ceremony hardening | `tools/cut-release.sh` pre-cut live-verify exercised on every v4.4.x cut since v4.4.4 | ✅ |

---

## Operator follow-up items

None blocking. The 3 remaining observations (F-OBS-001/002/003) are intentionally not filed as fix candidates:

- **F-OBS-001** — nginx config has pre-existing `listen ... http2` deprecation warnings on the shared stack at `/home/dblagbro/docker/config/nginx/nginx.conf`. Out of scope for the proxy repo.
- **F-OBS-002** — `providers` and `api_keys` tables have more tombstoned rows on www1 than peers (originator-side history is richer; peers skip materializing rows they never saw). Active counts converge cluster-wide; this is design behavior in `app/cluster/sync.py:327-328`.
- **F-OBS-003** — `caller_memory` table has 0 writes from production traffic in 5+ days despite the feature flag being ON cluster-wide. Server side is correctly wired; the only consumer that flipped its `proxy_memory_enabled=true` (DevinGPT v2.74.51) doesn't appear to be sending the `X-Conversation-Id` header that gates writes. Operator watch item per `project_backlog_caller_memory_live_watch.md`.

---

## Backup artefacts

```
/home/dblagbro/backups/llm-proxy-v2-v4.4.0-20260520T204825Z.tar.gz
/home/dblagbro/backups/llm-proxy-v2-v4.4.1-20260520T211551Z.tar.gz
/home/dblagbro/backups/llm-proxy-v2-v4.4.2-20260520T215956Z.tar.gz
/home/dblagbro/backups/llm-proxy-v2-v4.4.3-20260520T222808Z.tar.gz
/home/dblagbro/backups/llm-proxy-v2-v4.4.4-20260520T233702Z.tar.gz
/home/dblagbro/backups/llm-proxy-v2-v4.4.5-20260521T000224Z.tar.gz
/home/dblagbro/backups/llm-proxy-v2-v4.4.6-… .tar.gz
/home/dblagbro/backups/llm-proxy-v2-v4.4.8-… .tar.gz
/home/dblagbro/backups/llm-proxy-v2-v4.4.9-… .tar.gz
```

All ~1.5 MB each; all gzip-validated at creation time.

Docker Hub: `dblagbro/llm-proxy2:4.4.{0..6,8,9}` + `:latest` all pushed.

GitHub releases: tags v4.4.0 through v4.4.9 (skipping v4.4.7) all created via `gh release create`.

---

## Verdict

**The v4.4.x cycle is operationally healthy and the backlog is at zero actionable items.** Recommended next action: **pause**. Resume only when (a) new traffic patterns emerge, (b) a consumer-side rollout exercises new surface area (e.g. caller-memory write-back when DevinGPT starts emitting `X-Conversation-Id`), or (c) a future deploy / incident surfaces something new.
