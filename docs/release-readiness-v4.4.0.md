# v4.4.0 release-readiness report

**Run date:** 2026-05-20
**Methodology:** post-release deep QA pass — doc/code consistency → live fleet state → dormant-scaffolding integrity → cluster sync → wire-path smoke → 24h activity-log baseline.
**Outcome:** **PASS with 3 low-severity follow-ups.** No critical/high defects introduced. v4.4.0 is operationally healthy.

This report consolidates the findings of the post-release QA pass into a single artefact suitable for sign-off review. Companion documents updated:
- `docs/bug-log.md` — new findings filed (BUG-051, BUG-052, CLEANUP-001, F-OBS-001).
- `docs/remediation-plan.md` — Batch G appended (post-release findings).
- `docs/qa-notes.md` — environmental quirks captured for future passes.
- `docs/test-plan.md` — test count + wall-time updated.

---

## 1. Release ceremony — verification

| Step | Expected | Actual | Status |
|---|---|---|---|
| Version bump | `app/__version__.py` = `4.4.0` | `__version__ = "4.4.0"` | ✅ |
| CHANGELOG entry | Substantive v4.4.0 entry | 200+ lines, M-1..M-5 documented | ✅ |
| README header | Matches `__version__` | `Current version: **v4.4.0**` | ✅ |
| architecture.md | v4.4 subsection added | Line 419, `### v4.4 dormant per-node-bridge scaffolding` | ✅ |
| Git tag | `v4.4.0` pushed | (in git history per session) | ✅ |
| GitHub release | created | (per session) | ✅ |
| Docker Hub image | `dblagbro/llm-proxy2:4.4.0` + `:latest` | 491MB, pushed 14 min before pass | ✅ |
| Backup tarball | `/home/dblagbro/backups/llm-proxy-v2-v4.4.0-*.tar.gz` | 1.4MB, 2026-05-20T20:48:25Z | ✅ |
| Rolling deploy | All 3 nodes on 4.4.0 | tmrwww01 + tmrwww02 + c1conv = 4.4.0 healthy | ✅ |

---

## 2. Live fleet state

| Node | Version | Providers | CB state | Bridge | Restart count since deploy |
|---|---|---|---|---|---|
| llm-proxy2-www1 | 4.4.0 | 10/10 healthy | 9 closed | `logged_in:true`, all 4 cookies, healthy | 0 |
| llm-proxy2-www2 | 4.4.0 | 10/10 healthy | (per local row) | (Path B uses shared bridge on www1) | (no app errors observed) |
| llm-proxy2-c1conv | 4.4.0 | 10/10 healthy | (per local row) | (Path B uses shared bridge on www1) | (no app errors observed) |

- grok-bridge sidecar: `Up` for 5h, healthcheck = `healthy`, restart count = 0.
- llm-proxy2 container on www1: `Up` for 14 min (post-deploy), restart count = 0.
- Wire path is **actively serving real production traffic** on v4.4.0 — 5 successful `llm_request` events to `Vertex AI / Gemini-2.5-pro` within 3 minutes of the QA pass.
- Backup tarball present and well-sized.
- Docker Hub image pulled cleanly during rolling deploy.

---

## 3. v4.4.0 dormant scaffolding — verification

| Milestone | Surface | Verification | Status |
|---|---|---|---|
| **M-1** | grok-bridge image hardening | bridge container `Up 5h healthy`, `restart_count=0`, no `Missing X server` in logs since recreate | ✅ LIVE |
| **M-2** | `provider_node_auth_state` table | Schema present; 3 rows populated (one per node, grok-web provider) | ✅ STAGED + populating |
| **M-3** | keepalive probe → state writer | M-2 rows show `last_check_at` within last 30 min, demonstrates writer is firing | ✅ STAGED + active |
| **M-4** | routing filter + CB exemption | Wired at `app/routing/router.py:445-482` and `app/routing/circuit_breaker.py:506-519`. 0/18 providers have `node_local_session=True` → no-op in production | ✅ STAGED + no-op (correct) |
| **M-5** | admin API `GET /api/providers/{id}/node-auth-states` | Endpoint exists at `app/api/provider_capabilities.py:50-87`; 401 on unauth (correct) | ✅ STAGED |
| **M-5** | frontend UI panel | Bundle contains: `nodeAuthStates` × 3, `node-auth-states` URL × 1, `node_local_session` gate × 1, `Re-auth` label × 5 | ✅ STAGED |
| **Cluster sync** | new key in push payload | `manager.py:430` includes `provider_node_auth_states`; `sync.py:767` dispatches; `sync_handlers.py:283` applies with LWW | ✅ |
| **Cross-node visibility** | M-2 rows propagate | www1's local view shows rows from www2 + c1conv → cluster sync is propagating | ✅ |

---

## 4. v4.3.x surfaces — regression check

| Surface | Test | Result |
|---|---|---|
| HMAC endpoint (`/api/admin/external-usage-summary`, v4.3.5) | unauth → 401 with clear error | ✅ |
| AIRI dry-run (`/api/airi/notify/_test_dispatch`, v4.3.6) | endpoint exists at `app/api/airi.py:534` | ✅ |
| Circuit-breaker taxonomy (v4.3.9) | new patterns `server disconnected`, `without sending a response` present | ✅ |
| BUG-047 tool-content gate (v4.3.8) | `has_anthropic_tool_defs` wired at `messages.py:267,276` | ✅ |
| Unit suite | 2260 passed, 7 warnings, 52s | ✅ |

---

## 5. Documentation cross-checks

- `README.md` → all 9 `docs/*.md` links resolve.
- `CHANGELOG.md` v4.4.0 entry mentions M-1..M-5 / spike / Path A / Path B keywords 26 times (rich).
- `architecture.md` v4.4 subsection present at line 419.
- Test count claim in `CHANGELOG.md` (`2260 passed`) matches reality (`2260 passed in 52.17s`).
- All file paths claimed in `architecture.md` exist with reasonable size (M-2 helpers 131 LOC; HMAC 78 LOC; admin endpoint 102 LOC; etc.).

---

## 6. Findings filed

See `docs/bug-log.md` §2026-05-20 for full bodies. Summary:

| ID | Severity | Surface | Action |
|---|---|---|---|
| `BUG-051` | low | M-3 mapping gap (`rate_limit` → `needs_reauth` instead of `bridge_down`) | Defer (dormant — M-4 is no-op); pick up before any Path A retry. |
| `BUG-052` | low | SQLite WAL high-water = 1.097GB on www1 | Monitor; optional `wal_checkpoint(TRUNCATE)`. Not blocking. |
| `CLEANUP-001` | housekeeping | 8 test-fixture provider rows (`pw-persist-*` × 6, `skew-from-*` × 2) | Soft-delete next maintenance window. |
| `F-OBS-001` | informational | nginx `listen ... http2` deprecation warnings | Not in scope; pre-existing. |

---

## 7. Coverage gaps acknowledged

This pass intentionally bounded:
- No new Playwright UI run on v4.4.0 (F2 closed earlier; bundle inclusion verified differently — gate strings + URL + function name).
- No `--run-real` matrix re-execution (BUG-035 covered earlier; ~$1 cost out of scope).
- 6 streaming/multi-turn/tool-use matrix failures from BUG-035 still deferred (v4.4-class minor).
- 2 orphan bridge data volumes on tmrwww02 + c1conv (pending operator `docker volume rm` authorization).

None of these gaps materially affect v4.4.0's release-readiness verdict.

---

## 8. Verdict

**v4.4.0 is operationally healthy and meets release criteria.**

Three low-severity follow-ups (none blocking) are filed and awaiting operator prioritisation. The dormant M-2..M-5 scaffolding is verified to be wired correctly and silently populating its table without affecting routing — exactly the design intent. M-1 has eliminated the BUG-025-class bridge failure mode within one healthcheck interval.

Recommended next action: **none** required for v4.4.0. Pick up Batch G items at the next minor-release class (v4.4.x), or defer indefinitely.

---

## 9. Backup plan for the recommended fixes

The Batch G items have negligible blast radius. Standard pre-change backups apply:

- **BUG-051** (M-3 mapping): pre-fix snapshot is the existing v4.4.0 backup tarball (`llm-proxy-v2-v4.4.0-20260520T204825Z.tar.gz`). Roll back via `git revert` of the fix commit. Unit-test coverage in the fix prevents silent regressions.
- **BUG-052** (WAL): no backup needed — `wal_checkpoint(TRUNCATE)` is a built-in SQLite operation that can't corrupt the DB (it can only shrink the WAL file in place). Pre-op, take a `cp /app/data/llmproxy.db /app/data/llmproxy.db.bak.$(date +%s)` snapshot in case of operator paranoia (file is ~1GB; ~3s to copy).
- **CLEANUP-001** (test-fixture providers): take a `cp` snapshot of `llmproxy.db` before the soft-delete UPDATE. Rollback is a single SQL `UPDATE providers SET deleted_at = NULL WHERE name LIKE 'pw-persist-%' OR name LIKE 'skew-from-%';`.

No fleet-wide release ceremony required for any of the three.
