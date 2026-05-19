# Remediation Plan — consolidated (2026-05-19)

**Source documents:** `docs/bug-log.md` · `docs/test-plan.md` · `docs/qa-notes.md` ·
`architecture.md` · `design.md` · `refactor-log.md`. **Companion:**
`docs/backup-plan.md`. **Supersedes** the per-pass remediation entries
previously in this file (preserved in git history; the v4.3.0 + v4.3.2
fix groups are consolidated below).

> **Status: PLANNING ONLY — no fixes implemented.** This is the pause
> point per the QA discipline. Each batch requires operator approval
> before remediation begins.

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
| **BUG-026** | MEDIUM | `app/monitoring/keepalive.py` | wrong architectural premise (per-node sidecar vs shared public bridge) | dead code in v4.3.2 release; no behavioural effect |
| BUG-001 | LOW | `tests/unit/` mock fixture | shared mock state not drained between tests | 1 flaky test in full-suite runs |
| BUG-002 | LOW | `tests/mock_llm_server.py` | static port binding | 13 errors in concurrent suite runs |
| BUG-003 | LOW | `tests/integration/` | test cleanup leaves rows | prod-DB pollution with `pytest-mock` rows |
| (coverage) | LOW | TTS audible playback | headless Chromium has no audio device | only the manual `release-checklist.md` step verifies |
| (coverage) | LOW | voice buttons keyboard / mobile | not exercised by the v4.3.0 pass | a11y / responsive gap |
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
