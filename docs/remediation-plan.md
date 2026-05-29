# Remediation Plan — consolidated (last updated 2026-05-27)

**Source documents:** `docs/bug-log.md` · `docs/test-plan.md` · `docs/qa-notes.md` ·
`architecture.md` · `design.md` · `refactor-log.md`. **Companion:**
`docs/backup-plan.md`. **Supersedes** the per-pass remediation entries
previously in this file (preserved in git history; the v4.3.0 + v4.3.2
fix groups are consolidated below).

## 2026-05-28 — QA-pass remediation arc COMPLETE (v4.4.24 → v4.4.28)

The deep QA pass's findings have been remediated across 5 releases. **All Priority 0 + Priority 1 items closed.** Priority 2 cosmetic/a11y items also closed (F-OBS-005 fully; F-OBS-004 worst-case + dark-mode tertiary sweep). Remaining backlog: F-INFRA-002 (Playwright stale test) + any future operator-discretionary contrast/a11y depth.

| Release | Findings closed |
|---|---|
| **v4.4.24** | BUG-079 (guard + de-dup) · BUG-080 · BUG-081 · BUG-083 · F-INFRA-001 |
| **v4.4.25** | BUG-084 (api_keys INSERT field coverage, surfaced during v4.4.24 verification) |
| **v4.4.26** | F-OBS-005 (a11y, all 9 pages clean) · F-OBS-004 (worst-case 1.84→3.42) |
| **v4.4.27** | BUG-079 PERMANENT FIX (UNIQUE constraint + idempotent migration) |
| **v4.4.28** | F-OBS-004 dark-mode tertiary sweep (3.03→3.42 across 29 files) |
| pending | BUG-082 (hub-side, proxy exonerated, memo presented for forward) |

### v4.4.27 — observation that validated the work

Between v4.4.24's manual cleanup of www2's 1 duplicate row and v4.4.27 prep, www2 silently accumulated **3 NEW dup groups in 24h**. The check-then-insert race was still live under the `.limit(1)` guard — the guard prevented the crash, not the cause. UNIQUE INDEX closed it at the schema level. Net release impact across the arc: cluster-sync data correctness restored from "silently broken" to "schema-impossible to break."

### Backup procedure executed as planned

Per `docs/backup-plan.md`'s v4.4.27 addendum, fresh per-node snapshots were taken before the data-fix portion of the v4.4.27 migration. Snapshots retained: `/home/dblagbro/backups/llmproxy.{www1,www2,c1conv}.pre-v4427.*` (~30 MB each on peers, ~1.1 GB on www1 due to bigger activity_log). The backup-plan's rollback procedure remains valid if a future migration goes sideways.

---

## 2026-05-27 — new findings from deep QA pass (historical; superseded by the consolidated 2026-05-28 entry above)

**Source:** `docs/bug-log.md` 2026-05-27 section (BUG-079..BUG-083 + F-OBS-004..006 + F-INFRA-001).
**Status:** PLANNING ONLY — pause before fixes per QA-pass protocol.

### Priority 0 — release blocker (data correctness)

| ID | Title | Severity | Fix scope | Notes |
|---|---|---|---|---|
| BUG-079 | Cluster sync silently broken for ~6d on www2 + c1conv | HIGH | hotfix code + data fix + schema migration | Critical-path for v4.4.18+ cluster-sync arc to actually do anything |
| BUG-080 | 5 of 7 apply handlers share the same vulnerability pattern | HIGH | hotfix code (5 `.limit(1)` adds) | Best fixed in the same release as BUG-079 |
| BUG-081 | `push_sync` doesn't inspect peer response status | MEDIUM | one-line code change + 1 unit test | Hardening that prevents the next BUG-079-class incident from going undetected |

**Suggested release**: v4.4.24 — "cluster-sync apply robustness". Three commits:

1. Hotfix: `.limit(1)` on all 5 vulnerable handlers + regression tests
2. Data fix: 2-step SQL script de-duping the existing duplicate rows on www2 + c1conv (run manually after deploy, not embedded in the migration — surgical)
3. Hardening: `push_sync` response-status check + warning log on non-200
4. Schema migration: idempotent ALTER chain to add UNIQUE indexes via the SQLite "shadow table" pattern. **DEFERRED to v4.4.25** — schema changes carry separate backup needs.

**Retest scope after v4.4.24:**
- Drive the same live LWW test (mint key on www1, observe propagation to peers in <70s)
- Verify peer logs show `200` on incoming `/cluster/sync` post-deploy
- Run the duplicate-detection probe across all 3 nodes; counts should converge over the next 60s post-sync

### Priority 1 — operational + cross-team

| ID | Title | Severity | Fix scope |
|---|---|---|---|
| BUG-082 | F2 cache_control breakpoint not producing upstream cache hits | MEDIUM (cross-team) | draft memo for hub team |
| BUG-083 | `Query(hours)` accepts negative values | LOW | `ge=1` add + 1 test |

**Suggested handling:**
- BUG-082: draft memo for operator to forward to hub team. No proxy-side change needed unless hub asks for help.
- BUG-083: small one-line fix that can ride in v4.4.24.

### Priority 2 — hardening / a11y / test infra

| ID | Title | Severity | Fix scope |
|---|---|---|---|
| F-OBS-004 | Light-mode contrast smell on `text-gray-400` labels | LOW (a11y) | sed pass on `frontend/src/pages/` |
| F-OBS-005 | Light a11y attribute coverage on shared UI | enhancement | dedicated a11y pass on `frontend/src/components/ui/` |
| F-INFRA-001 | Unit-test conftest hits live prod at sessionfinish | LOW | scope to integration only OR gate behind env var |

**Suggested handling:** combine into a single "v4.4.x hardening" PR. Low-risk.

### Priority 3 — informational / tracking

| ID | Title | Status |
|---|---|---|
| F-OBS-006 | Async-session tracer is live but unverified live (no leaks yet) | tracking — needs a real leak |
| F-OBS-002 | Tombstoned-row count drift across nodes | design behavior, will get worse if BUG-079 stays open |
| F-OBS-003 | Caller-memory write-back gated on `X-Conversation-Id` | watching cron `/home/dblagbro/bin/devingpt_header_verify.sh` |

### Dependencies between fixes

- BUG-081 (response status check) should ship BEFORE BUG-079's data fix is applied — so if anything goes wrong during the de-dup, sync errors surface immediately.
- BUG-079 data fix has 2 nodes affected; do **www2 first**, verify sync works to www2, **then c1conv**. Don't batch.
- UNIQUE-constraint migration depends on the data fix completing — duplicates must be gone before the constraint is enforceable.
- Backup-plan.md update needs to land before v4.4.24 deploy (the de-dup hard-deletes rows, which is irreversible without a DB restore).

### Risky changes requiring extra caution

- **Data fix on www2/c1conv** — hard DELETE of rows from `provider_ai_review`. The deleted row carries informational AI-review verdicts; safe to drop the row with NULL lifecycle fields. **Snapshot the table on each peer before the delete** (see `docs/backup-plan.md` update).
- **UNIQUE constraint migration via shadow-table** — full table rewrite, brief read-only window. Test on a staging DB first.

---



> **Status (2026-05-19): Batch A attempted and DEFERRED to Batch C
> (v4.4 arc).** The other batches remain planning-only and await
> operator approval before remediation begins. See §13 below for the
> Batch A post-mortem.

---

## 1. Scope & purpose

A single risk-controlled fix plan covering **every currently open
finding** across:

- the **v4.3.0 deep QA pass** (2026-05-18, `docs/4.3-qa-report.md`),
- the **v4.3.2 post-deploy verification pass** (2026-05-19, `bug-log.md` §2026-05-19),
- the **long-standing test-infra items** from the v3.5.x QA (BUG-001/002/003), and
- the **v4.4 forward arc** (per-node bridge auth) included for
  traceability — it is architectural, not a defect.

## 2. System under remediation — current state

- **llm-proxy2 v4.3.2** live on all 3 nodes (tmrwww01, tmrwww02, c1conv).
- **whisper-bridge 4.3.0** unchanged; AIRI v4.0+v4.1+v4.2+v4.3 all
  shipped; v4.3.1 (QA remediation, Groups 1+2) shipped + verified ✅.
- **v4.3.2** (BUG-023 interim noise patch) shipped but is a **no-op in
  production** — BUG-026.
- **grok-web is failing fleet-wide**: provider `8beb17c4bd11de26`
  (`Grok-Web-Devin`) CB tripped on all 3 nodes; root cause is BUG-025
  (the shared grok-bridge on tmrwww01 has a crashed Playwright page
  and an HTTP server that refuses connections, while `docker ps`
  still reports the container as `Up 10 days`).
- Other 9/10 providers healthy fleet-wide.
- `tests/unit/` 2133 green; `TestAiriTTS` passes against live; the
  v4.3.1 `/api/auth/session` endpoint verified live across the fleet.

## 3. Inventory of open items

| ID | Severity | Subsystem | Root-cause area | Surface area / blast radius |
|---|---|---|---|---|
| **BUG-025** | HIGH | grok-bridge sidecar | container outer process alive, **inner service dead** (no healthcheck) | grok-web requests + probes fail fleet-wide (cluster-synced CB state) |
| ~~BUG-026~~ | MEDIUM | `app/monitoring/keepalive.py` | wrong architectural premise (per-node sidecar vs shared public bridge) | ✅ **LIVE v4.3.4 (2026-05-19)** — Batch B revert option, all 3 nodes |
| BUG-001 | LOW | `tests/unit/` mock fixture | shared mock state not drained between tests | 1 flaky test in full-suite runs |
| BUG-002 | LOW | `tests/mock_llm_server.py` | static port binding | 13 errors in concurrent suite runs |
| BUG-003 | LOW | `tests/integration/` | test cleanup leaves rows | prod-DB pollution with `pytest-mock` rows |
| (coverage) | LOW | TTS audible playback | headless Chromium has no audio device | only the manual `release-checklist.md` step verifies |
| (coverage) | LOW | voice buttons keyboard / mobile | not exercised by the v4.3.0 pass | a11y / responsive gap |
| ~~BUG-027..033~~ | LOW | (coverage) | UI pages, forms, persistence, cache, mobile, a11y | **CLOSED 2026-05-19 via F2** (BUG-031 deferred for SMTP test-mode flag) |
| ~~BUG-034~~ | LOW | (coverage) | full integration suite | **CLOSED 2026-05-19 via F3** — 66 pass / 0 fail across 2 runs |
| ~~BUG-035~~ | LOW | (coverage) | real-provider matrix | ✅ **RAN 2026-05-19** (1 pass / 12 fail / surfaced BUG-043/044/045/046) |
| ~~BUG-036~~ | LOW | (coverage) | rollback drill | ✅ **DRILLED 2026-05-19** — 3 image cycles, ~13s each, PASS — `docs/backup-plan.md` |
| ~~BUG-037~~ | LOW | (coverage) | version-skew test | ✅ **EXERCISED 2026-05-19** — controlled prod skew ~146 s, 5/5 assertions PASS — `docs/qa-notes.md` |
| ~~BUG-038..040~~ | LOW/MED | (doc gap) | CB-sync, public-URL hairpin, activity-log scope | **CLOSED 2026-05-19 via F1** — `architecture.md` §"Cluster sync" + §"grok-bridge sidecar" updated |
| ~~BUG-041~~ | MED | `app/api/apikeys.py` | missing `ge=0` on numeric cap fields | ✅ **LIVE v4.3.3 (2026-05-19)** — all 3 nodes |
| ~~BUG-042~~ | MED | `app/api/users.py` (user create) | missing min-length validator on password | ✅ **LIVE v4.3.3 (2026-05-19)** — all 3 nodes |
| (arc) | — | sidecar-dependent providers | shared single-bridge URL is fragile; no per-node auth model | v4.4 design + multi-milestone build |

**Closed-and-verified (historical context):**
v3.5.x BUG-004..015 closed by v3.5.8..v3.5.11; BUG-016..018 by v3.7.15;
BUG-019 (critical admin lockout) closed by v3.7.14; v4.3.0 QA-pass
BUG-020/021/022/024 closed by v4.3.1; BUG-023 reclassified as
resolved-by-BUG-025 (the "missing local sidecar" diagnosis was wrong
— see §11 *Lessons*).

## 4. Risk-controlled priority order

Ordered by **operational impact × ease of containment**. Lower-numbered
items run first, gating later batches.

1. **Batch A — restore grok-web** (one operational action on
   tmrwww01). Currently grok-web is failing fleet-wide; this is the
   highest-impact still-bleeding finding. Restart of a single named
   sidecar; near-zero blast radius. **Unblocks: Batch B/C scoping.**
2. **Batch B — eliminate dead v4.3.2 code + harden the bridge container**
   (one llm-proxy2 release + one compose change per node). Code is
   currently dead but adds reader-confusion to the codebase; the
   compose healthcheck prevents the next BUG-025 from sitting silent
   for 10 days. Frontend-/keepalive-only change; full rollback path
   via `dblagbro/llm-proxy2:4.3.1` retag.
3. **Batch C — v4.4 per-node bridge auth** (architectural arc). Not a
   defect — a forward design. Planned to land *after* the empirical
   "does Grok tolerate multi-session" spike confirms the architecture
   choice (per-node duplicate sessions vs cluster-shared bridge).
   Multi-milestone build under its own design doc.
4. **Batch D — long-standing test-infra (BUG-001/002/003)**. Not
   user-visible; only affects the developer experience of running the
   full suite. Local code changes inside `tests/`; can ship in any
   patch release.
5. **Batch E — QA-process / observability hardening**. Tighten the
   release-ceremony to catch a v4.3.2-style misship before it ships
   (live verification of code paths under the actual provider config,
   sidecar inner-service health check); add the small TTS audible-
   playback automation if practical. Pure process + tests / docs.
6. **Batch F — coverage gaps inventory** (BUG-027..040, added 2026-05-19,
   see §14). F1 (doc gaps) DONE 2026-05-19; F2 (UI/a11y/mobile)
   DONE 2026-05-19 with BUG-031 deferred + 2 real defects (BUG-041,
   BUG-042) surfaced. F3 partial: BUG-034 DONE 2026-05-19 (suite is
   clean); BUG-035 / BUG-036 / BUG-037 have step-by-step runbooks in
   `docs/f3-runbooks.md` awaiting operator-time. Zero-risk
   additions / drills.

## 5. Fix batches (grouped by subsystem & root cause)

### Batch A — grok-web (operational, fleet-blocker)

| | |
|---|---|
| **Items** | BUG-025; BUG-023 (resolved-by). |
| **Subsystem** | `llm-proxy2-grok-bridge` sidecar on tmrwww01. |
| **Root cause** | Inner FastAPI/Playwright service crashed while the container's outer entrypoint stayed alive; no inner-service healthcheck. |
| **Fix scope** | **Operational**, no code. `docker restart llm-proxy2-grok-bridge` on tmrwww01 (single named container, `--no-deps` not even needed because the command is not `compose up`). Single shell command. |
| **Effort** | ≤ 1 min. **Risk:** very low (the failure mode is already in effect; restart can only improve it). |
| **Dependencies** | None. Unblocks Batch B/E scoping (so Batch B's bridge healthcheck has a known-good baseline). |

### Batch B — eliminate dead code + bridge healthcheck (small, local + ops)

| | |
|---|---|
| **Items** | BUG-026 (decide: revert or correct premise) + a compose-level healthcheck on `grok-bridge`. |
| **Subsystems** | `app/monitoring/keepalive.py`, `tests/unit/test_v432_no_local_sidecar.py`; `docker-compose.yml` on tmrwww01 (the only node that runs the bridge). |
| **Root cause** | Architectural misread of grok-web at remediation time (shared public-URL bridge vs per-node sidecar); the patch's gate is unreachable in production. |
| **Recommended option** | **Revert** the v4.3.2 keepalive.py addition and the unit test that locked it in. Once Batch A clears the underlying grok-bridge failure, the noise the patch was trying to suppress no longer exists; keeping dead code violates `design.md` §"One responsibility per file" + the codebase legibility north star. The `_local_sidecar_reachable` helper can be kept *iff* a real future caller emerges; otherwise it goes with the revert. |
| **Alternative option** | If we want to keep a sidecar-reachability primitive for v4.4, repurpose the gate to detect a real `ConnectError` from the actual probe attempt (post-hoc) rather than a speculative pre-check. Adds value only if the v4.4 shape needs it — defer this decision until the v4.4 design fixes the abstraction. |
| **Healthcheck addition** | `healthcheck:` block on the `grok-bridge` service in `/home/dblagbro/docker/docker-compose.yml` that probes the inner FastAPI on `:8000/health` or `:8000/status` every 30 s with a small retry count; `restart: unless-stopped` already in place will then auto-recover. |
| **Fix scope** | **Local code change + local compose change.** Releases as v4.3.3 (frontend dist unchanged; only the keepalive revert + compose). |
| **Effort** | 1–2 h including the release ceremony. **Risk:** low (revert is mechanically simple; the compose healthcheck only adds a watchdog). |
| **Dependencies** | Batch A must clear first so the new healthcheck doesn't restart-loop a freshly-brought-up bridge that's still in the original crashed state. |

### Batch C — v4.4 per-node bridge auth (architectural, forward design)

| | |
|---|---|
| **Items** | The v4.4 arc (operator-spec, 2026-05-19). |
| **Subsystem** | Architectural — touches `providers/grok_web.py`, the routing-side dispatch path, frontend provider-edit UX, and per-node auth state (new table). |
| **Root cause area** | The current shared-bridge-via-public-URL is fragile (cross-internet hairpin, single point of failure, no per-node auth-state visibility, no guided cross-node auth UX). |
| **Fix scope** | **Architectural** — multi-milestone, design-doc-first per operator directive ("design it and do it once, no rush"). |
| **Open empirical question** | Whether Grok tolerates 2–3 concurrent logged-in browser sessions on the same account from different datacenter IPs. A short observation spike answers this before the design is committed. |
| **Effort** | Weeks (design + 3 milestones). **Risk:** moderate — touches synced provider config, the routing path, and the auth UX. |
| **Dependencies** | Not gated on A or B, but most sensibly happens *after* both (clean baseline; no dead-code distractions). |

### Batch D — long-standing test-infra nits

| | |
|---|---|
| **Items** | BUG-001 (test isolation flake), BUG-002 (mock LLM server port), BUG-003 (integration tests pollute prod DB). |
| **Subsystem** | `tests/unit/`, `tests/mock_llm_server.py`, `tests/integration/conftest.py`. |
| **Root cause** | Shared mutable state in fixtures (BUG-001), static port allocation (BUG-002), missing test-cleanup paths (BUG-003). |
| **Fix scope** | **Local** — all changes inside `tests/`; no production code touched. |
| **Effort** | 2–4 h. **Risk:** very low (tests only). Can ship in any subsequent release alongside other changes. |
| **Dependencies** | None. Independent. |

### Batch E — QA-process / observability hardening

| | |
|---|---|
| **Items** | (1) Pre-cut release-ceremony step: live-verify the changed code path actually fires under the real provider config (would have caught BUG-026). (2) Strengthen `release-checklist.md`: explicit "probe the sidecar's *inner* service, not just `docker ps`" check (would have caught BUG-025 earlier). (3) Optionally: a Playwright-driven audible-TTS check via Chromium's `--use-fake-ui-for-media-stream` + audio routing if a reasonable approach exists (BUG-022 / coverage). |
| **Subsystem** | `docs/release-checklist.md`, `tools/cut-release.sh`, possibly a small ceremony helper script. |
| **Root cause** | Process gap — the v4.3.0 deep QA was thorough but the v4.3.2 patch did not get an equivalent pre-cut verification (it had unit tests + a syntax-correct release, but the gate the patch added was never exercised against the real `bridge_url` value). |
| **Fix scope** | **Local + doc.** |
| **Effort** | 1–2 h. **Risk:** none — docs / scripts. |
| **Dependencies** | None. Lands in any patch. |

## 6. Local vs architectural classification

| Batch | Local | Architectural | Notes |
|---|---|---|---|
| A — restore grok-web | ✓ (ops) | — | Single-container restart. |
| B — dead-code + healthcheck | ✓ | — | Reverts an existing patch; adds a compose healthcheck. |
| C — v4.4 per-node auth | — | ✓ | New synced auth-state table, new UI flow, fleet-wide grok-bridge deploy or shared-bridge formalization. |
| D — test-infra | ✓ | — | Tests-only. |
| E — process hardening | ✓ (doc/script) | — | No product code. |

Only **Batch C** is genuinely architectural.

## 7. Backups required before each batch

(Full procedure in `docs/backup-plan.md`. Per-batch deltas:)

| Batch | What to back up | Where | Why |
|---|---|---|---|
| A | None code-side. Optional: `docker logs llm-proxy2-grok-bridge` snapshot (the crash stack-trace is the only durable evidence of the failure mode for the post-mortem). | local file | Restart loses the bridge's runtime logs unless captured. |
| B | The `v4.3.2` Docker image is already on Docker Hub (`dblagbro/llm-proxy2:4.3.2`). Compose snapshot per node: `cp /home/dblagbro/docker/docker-compose.yml{,.bak-pre-v433}` on tmrwww01 + tmrwww02; `/opt/C1/instance/docker-compose.yml` on c1conv. | per-node | Rollback target is `dblagbro/llm-proxy2:4.3.1` (already on Hub, digest `d2d7cf15`). |
| C | Before any v4.4 implementation: branch off the then-current release tag (`git checkout -b v4.4-work v4.3.x-tip`). Provider-config DB snapshot (because the new `provider_node_auth_state` table is added; rollback may need to drop it). | git + DB | Architectural change deserves a clean rollback path. |
| D | None — code lives entirely in `tests/`. | — | No production data touched. |
| E | None — docs/scripts only. | — | — |

## 8. Rollback expectations per batch

| Batch | Rollback method | Time to restore | Verification |
|---|---|---|---|
| A | `docker restart llm-proxy2-grok-bridge` is itself the recovery; if the restart leaves the bridge worse, restart again or recreate from the same image tag pinned in compose. | seconds | `curl http://llm-proxy2-grok-bridge:8000/status` from inside `llm-proxy2` returns 200; one grok-web probe succeeds; CB `8beb17c4` returns to `closed/0`. |
| B | Retag `dblagbro/llm-proxy2:4.3.1` → `llm-proxy2:latest` on each node + `docker compose up -d --force-recreate --no-deps llm-proxy2`. Compose healthcheck removal: restore from the `.bak-pre-v433` snapshot. | ~5 min per node | `/health` reports `version:4.3.1`; fleet stays serving. |
| C | Revert to the pre-v4.4 image tag; drop the new auth-state table (or leave it dormant — additive, so leaving it is also safe). | ~10 min per node (image roll) + DB step | `/health` reports the prior version; provider listings still 10/10. |
| D | Revert the test-only commit. | seconds | `pytest tests/unit/` still green. |
| E | Revert the doc/script commit. | seconds | n/a — process change only. |

## 9. Retest scope per batch

| Batch | Retest |
|---|---|
| A | (a) Bridge `/status` returns 200 from inside `llm-proxy2`. (b) One real grok-web request through `/v1/messages` or via the activity log shows a `severity=info` keepalive_probe row instead of `error`. (c) CB `8beb17c4` returns to `closed/0` on all 3 nodes (cluster sync). |
| B | (a) Full unit suite (must stay 2133+ green ignoring the deleted `test_v432_no_local_sidecar.py`). (b) `TestAiriTTS` Playwright test still passes. (c) `/health` returns v4.3.3 on all 3 nodes. (d) Healthcheck verification: kill the bridge's inner process manually, observe automatic container restart within the healthcheck window. |
| C | A full v4.3.0-style deep QA pass — every milestone smoke-tested live; provider-config sync verified; the new per-node auth UI walked through end-to-end on a real browser; cluster sync of the new table verified. |
| D | `pytest tests/unit/ tests/integration/ -q` clean in a single invocation (no order-dependent flakes; no Address-already-in-use; no leftover `pytest-mock` rows in the prod DB). |
| E | Run the new release-ceremony verification once on a small staged change; confirm it would catch a reproduction of BUG-026 (e.g. by adding a gate that depends on a hypothetical config and intentionally seeding mismatched config). |

## 10. Risky changes flagged

- **None of A, B, D, E carry real risk** individually. The riskiest single
  call is in Batch B if we choose the *alternative* (correct the
  v4.3.2 gate rather than revert). That would be working code that
  depends on the still-fragile shared-bridge architecture; would
  obscure the v4.4 arc by leaving an incomplete primitive in place.
  **Recommendation: revert, not correct.**
- **Batch C is inherently architecturally risky** because it touches
  synced provider config, the routing path, and the auth UX. It
  deserves a design doc + a multi-session build; do not rush.
- **Compose changes (Batch B's healthcheck)** are per-node — must be
  applied identically on tmrwww01, tmrwww02, and c1conv to keep the
  fleet uniform. Update them as part of the same patch release and
  use the per-node `.bak-pre-v433` snapshots as the rollback path.

## 11. Lessons distilled from the QA passes (for `qa-notes.md`)

Already recorded in `qa-notes.md` 2026-05-19 entry; reproduced here so
the plan stands alone:

1. **Container "Up" ≠ service healthy.** `docker ps` is *not* a
   health signal for a sidecar with an inner service. Probe the inner
   service explicitly (an HTTP `/status` is enough). BUG-025 hid in
   that gap for ≥10 days.
2. **Read the live provider config before targeting a sidecar fix.**
   BUG-026 was avoidable: a 30-second `SELECT extra_config FROM
   providers` would have shown the `bridge_url` was a public URL and
   invalidated the patch's premise before any code was written.
3. **Coverage of "the gate actually fires."** A unit test that
   asserts a flag flips, plus a green test suite, plus a successful
   release ceremony — all together are *not* a verification that the
   gate would ever be reached in production. Add a real-config live-
   verification step to the release ceremony.

## 12. Stop point

**No fix has been implemented.** This is the explicit pause point per
the QA discipline. The next action is operator review of this plan —
specifically:

- Confirm Batch A ordering / authorization to restart the bridge.
- Choose Batch B's option: **revert** (recommended) vs **correct the
  v4.3.2 gate**.
- Decide whether Batch D and Batch E ride along in the same v4.3.3
  release, or wait for a quieter window.
- Schedule Batch C — when the v4.4 design work starts and who runs
  the empirical Grok-multi-session spike.

---

## 13. Batch A post-mortem (2026-05-19)

Operator authorised Batch A (`docker restart llm-proxy2-grok-bridge` on
tmrwww01) per the plan's "restart restores the bridge" expectation. The
restart **escalated the failure rather than fixing it**:

- `docker restart` → container went into a crash-loop (`exit 3`,
  RestartCount climbing) with `ERROR:ozone_platform_x11.cc(244)] Missing
  X server or $DISPLAY` on every cycle.
- Follow-up operator authorisation: clear `/data/playwright-state` and
  start fresh (cookies + Chromium profile preserved as tarball at
  `/tmp/grok-bridge-playwright-state-bak-20260519T183844Z.tar.gz`).
  **Same crash, same error** — the persisted state was not the cause.
- `docker stop` issued to halt the loop. Container now `Exited (3)`,
  cleanly stopped, no log/CPU thrash.

**Revised diagnosis:** the grok-bridge image carries a latent **startup
race** between Xvfb (display server) and the FastAPI lifespan
(`launch_persistent_context`). The 10-day-old "Up" container had won
that race on its original boot; every fresh container exit/restart
loses it. The 10-day "silent inner crash" (Playwright Page.goto crashed
during a probe) and the fresh-start crash-loop are **two distinct
failure modes** of the same fragile orchestration.

**Operator decision (2026-05-19): defer to the v4.4 arc.** The v4.4
per-node-auth design will redesign this layer anyway and may switch to
a fresh-context-per-request model that sidesteps Xvfb entirely; patching
`start.sh` / supervisord ordering now is throw-away work. grok-web is a
tertiary fallback and the rest of the proxy is fully healthy on all 3
nodes — the cost of the deferral is grok-web stays disabled until v4.4
ships.

**Lessons** (`qa-notes.md` to be appended later — captured here for the
moment): a plan-stated "near-zero risk" operational fix can still
surface a latent image-level defect that the original symptom hid. The
remediation-plan template should include a "**worst-case if this
turns out to be a deeper bug**" column for ops fixes, not just rollback
+ retest.

**Batches B / D / E remain planning-only**, awaiting operator approval.
Batch C is now the umbrella resolution path for both BUG-025 and the
underlying grok-web architecture; the v4.4 design doc will reflect this.

---

## 14. Coverage gaps inventory (Batch F — added 2026-05-19)

Operator-requested 2026-05-19: formally capture every QA-prompt surface
that was *bounded out* of the v4.3.0 deep QA + the v4.3.2 post-deploy
verification, so a future session sees explicitly what's untested and
the bug queue is complete-by-construction. Logged in `bug-log.md` §
2026-05-19 as **BUG-027 .. BUG-040**.

These are **coverage findings**, not defects. No failure has been
observed because no test has been run. They exist so:

1. A future deep-QA pass starts from an honest baseline of what
   *has* been tested vs not.
2. The v4.4 design (Batch C) starts from a **correct** `architecture.md`
   (BUG-038/039/040 are doc gaps that, left in place, will lead another
   diagnostician to re-make the BUG-026-class mistake).

### Sub-batch F1 — doc gaps (high-leverage, prerequisite to Batch C) — **DONE 2026-05-19**

| | |
|---|---|
| **Items** | BUG-038 (CB-sync semantics), BUG-039 (public-URL hairpin), BUG-040 (activity-log per-node scope). |
| **Subsystem** | `architecture.md` (3 small additions). |
| **Effort** | ~1–2 h. **Risk:** zero (docs only). |
| **Dependencies** | None. **Strongly recommended before Batch C starts** — the v4.4 design must start from an accurate baseline of the current grok-web architecture. |
| **Status** | **CLOSED 2026-05-19.** `architecture.md` §"Cluster sync" now carries a "What syncs cluster-wide vs what stays node-local" table (closes BUG-038 + BUG-040 with the asymmetry called out). §"grok-bridge sidecar" "Cross-node reachability" subsection has been replaced with "Sidecar topology — there is exactly ONE grok-bridge in the fleet" (closes BUG-039 + makes BUG-026's prevention explicit). Prerequisite for Batch C v4.4 design is satisfied. |

### Sub-batch F2 — UI / a11y / mobile coverage (multi-session) — **DONE 2026-05-19 (BUG-031 deferred)**

| | |
|---|---|
| **Items** | BUG-027 (admin UI pages), BUG-028 (form-validation depth), BUG-029 (data persistence + reload), BUG-030 (cache live), BUG-031 (notifications dispatch), BUG-032 (mobile/responsive), BUG-033 (deep keyboard a11y). |
| **Subsystem** | New Playwright tests under `tests/integration/test_playwright_ui.py`; new responsive sweep + keyboard-only walkthrough harnesses. |
| **Effort** | 1–2 full QA sessions; can be split per sub-area. **Risk:** zero (test additions only). |
| **Dependencies** | None. Lands incrementally — each item is independent. |
| **Status** | **CLOSED 2026-05-19.** 23 new test cases added across 8 new Playwright classes — all green against live deployment. Coverage gap closed for BUG-027/028/029/030/032/033. **BUG-031 deferred** pending a `dry_run` flag in the AIRI notifier (live SMTP send would spam operator's inbox; documented inline in test file). **F2 surfaced 2 real API validation defects: BUG-041 + BUG-042** (negative `rate_limit_rpm` and empty password both persisted by API). Tests for these defects are `xfail(strict=False)` until the API validators are tightened. |

### Sub-batch F3 — full-suite / drill / version-skew (one focused session) — ✅ **DONE 2026-05-19** (all 4 items executed)

| | |
|---|---|
| **Items** | BUG-034 (full `tests/integration/` end-to-end), BUG-035 (real-provider compatibility matrix `--run-real`), BUG-036 (rollback drill on a throwaway stack), BUG-037 (mixed-version cluster-sync skew test). |
| **Subsystem** | Existing tests + operational drill; no new code. |
| **Effort** | ~3–4 h focused session. **Risk:** F3a/b/d zero; F3c (rollback drill) carries the only real risk — but is run on a throwaway stack, not prod. |
| **Dependencies** | F3b spends $ on live providers → schedule as **pre-flight to the next minor release** (v4.4-class). F3c is most valuable *before* a high-stakes release where the rollback might actually be needed. |
| **Status** | **PARTIAL 2026-05-19.** BUG-034 closed — 2 consecutive clean runs (66 pass / 16 skipped / 0 failed); BUG-001/002/003 did not reproduce under current invocation (BUG-002 unreachable without `pytest-xdist`). BUG-035 / BUG-036 / BUG-037 each have a step-by-step runbook in `docs/f3-runbooks.md`; they close when the operator runs them (BUG-035: ~$1 / ~5 min before next release; BUG-036: ~30 min on a throwaway stack; BUG-037: 10-minute hold during next rolling deploy — no extra session needed). The session-time burden for closing F3 is therefore minimal: 1 invocation, 1 drill, 1 rolling-deploy ride-along. |

### Local vs architectural

All Batch F items are **local** (docs, tests, drills). None are architectural.

### Backups required

- F1: standard git commit + push. Doc-only.
- F2: standard test-add commits. No prod-data touched.
- F3a/b/d: no backups needed. F3c (rollback drill) is a deliberate
  rollback exercise — back up the throwaway stack's image tags + DB
  snapshot before starting; objective is to confirm the documented
  procedure restores them cleanly.

### Rollback expectations

Trivial for F1 and F2 (revert the commit). F3 has no rollback concept —
it either runs to completion or it doesn't.

### Retest scope

For F1: a future reader can follow the new `architecture.md` paragraphs
to a correct mental model of the grok-bridge / CB-sync / activity-log
architecture; spot-check by re-reading and seeing if BUG-026 would have
been avoidable from the doc alone.

For F2: each new Playwright test must run green on its first commit and
on every subsequent unit-suite cycle.

For F3: outcome documented in `docs/backup-plan.md` (drill timings,
verification results) and `qa-notes.md` (any new findings).

### Risky changes flagged

None. All zero-risk additions or contained drills.

### Stop point

**No coverage gap has been filled.** This inventory captures the gaps as
known unknowns; the actual closing of each gap waits on operator
prioritisation, just like Batches B–E.

### Why this batch matters

Without F1, the v4.4 design (Batch C) risks re-baking the same wrong
architectural assumption that produced BUG-026. Without F2/F3, the next
deep QA pass will repeat the v4.3.0 pass's scope — leaving the same
surfaces untested. The cost of filling these gaps is small relative to
the cost of another wasted release or another silent 10-day failure.

---

## 16. Batch G — v4.4.0 post-release findings (2026-05-20)

Filed after the v4.4.0 release-readiness QA pass (see `bug-log.md` §2026-05-20).
**No fix work is started.** This section is the operator-prioritised plan, awaiting approval.

| | |
|---|---|
| **Items** | BUG-051 (M-3 mapping gap for `rate_limit`/`billing`/`bad_request`/`unknown`); BUG-052 (WAL high-water observation); CLEANUP-001 (stale test-fixture providers). |
| **Subsystems** | `app/monitoring/keepalive.py` (BUG-051); `/app/data/llmproxy.db-wal` ops (BUG-052); `providers` table (CLEANUP-001). |
| **Root cause** | BUG-051: incomplete case mapping at M-3 design time (only 4 of 8 classifier outcomes considered explicitly). BUG-052: past write burst expanded WAL; SQLite preserves the file size by design. CLEANUP-001: test runs (Playwright UI + version-skew drill) leak provider rows that aren't garbage-collected. |
| **Recommended fix scope** | BUG-051: add explicit `rate_limit → bridge_down` branch in `keepalive.py:283-293`; leave `billing`/`bad_request`/`unknown` mapping policy as-is unless a re-auth signal would be wrong for them. ~5 LoC + 2 unit tests. BUG-052: optional one-shot `wal_checkpoint(TRUNCATE)` to reclaim file space (no code change; ad-hoc op). CLEANUP-001: soft-delete the 8 test rows via a one-shot SQL UPDATE, verify cluster sync picks up the tombstones. |
| **Effort** | BUG-051: 10 min code + 5 min review. BUG-052: 30s SQL pragma + monitor. CLEANUP-001: 5 min SQL + 5 min verify on peers. |
| **Risk** | All three are very low risk. BUG-051 only changes a dormant code path (M-4 is no-op for all 18 providers); the test will be a unit test, not a production probe. BUG-052 is a write to a single SQLite pragma. CLEANUP-001 uses soft-delete which is already supported by the sync layer. |
| **Dependencies** | None. All three independent of each other and of any other open batch. |
| **Backups required** | Standard release backup tarball + DB snapshot before CLEANUP-001 (the only one that mutates rows). BUG-051 covered by unit tests; BUG-052 doesn't change data. |
| **Rollback** | BUG-051: revert the commit. BUG-052: no rollback needed (file size is non-functional). CLEANUP-001: `UPDATE providers SET deleted_at = NULL WHERE name LIKE 'pw-persist-%' OR name LIKE 'skew-from-%';`. |
| **Retest** | BUG-051: new unit test asserts `rate_limit → bridge_down`; integration: run the c1conv probe with a 429 in flight and observe the row's `auth_state`. BUG-052: re-stat the WAL file after a TRUNCATE — should drop to <1MB. CLEANUP-001: verify `SELECT COUNT(*) FROM providers WHERE deleted_at IS NULL` drops to 10 (matches the legitimate provider count) on all 3 nodes. |
| **Stop point** | None of these are release-blockers. v4.4.0 is shipped and operationally healthy. Operator can defer indefinitely; recommended pickup point is the next minor-release class (v4.4.x). |
| **Status** | **CLOSED 2026-05-20.** All 3 items shipped: BUG-051 in v4.4.1 (M-3 rate_limit → bridge_down + 2 regression tests); BUG-052 in v4.4.4 (`PRAGMA wal_checkpoint(TRUNCATE)` wired into daily sweep + WAL high-water reclaim + 5 regression tests; pre-shipped one-shot ran in v4.4.1's session, dropped 1.097 GB → 4.1 MB); CLEANUP-001 done in v4.4.1 session via direct SQL (verified all 3 nodes converged to 10 active providers + 13 active keys; underlying BUG-053 cluster-sync gap surfaced and closed in v4.4.2). Plus opportunistic closures from the post-v4.4.x QA passes: BUG-053 / BUG-054 / BUG-055 / BUG-056 / BUG-057 / BUG-058 / F-OBS-004 all shipped through v4.4.2 → v4.4.9. Unit suite 2260 → 2288 (+28 regression tests). Backups `llm-proxy-v2-v4.4.{0..6,8,9}-*.tar.gz`. |

### Why this batch is small

The v4.4.0 release ceremony was thorough (rolling deploy, full doc update, fleet validated). The post-release QA pass found exactly what a healthy release should: three low-severity items that don't block anything and can be picked up at leisure. The absence of higher-severity findings is itself a release-quality signal.
