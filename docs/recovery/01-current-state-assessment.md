# llm-proxy-v2 — Current-State Assessment (Recovery, Stage 1)

**Author:** recovery assessment session · **Date:** 2026-08-06 · **Status:** Stage 1 (state reconstruction) IN PROGRESS
**Method:** targeted verification against repo, git, CI config, running containers, and logs. Facts are labeled; unverified claims are called out.

> Convention: **[FACT]** = directly observed this session · **[INFER]** = evidence-supported inference · **[OPEN]** = unresolved question.

---

## 0. TL;DR — the one thing that matters right now
**[FACT] The single asyncio event loop is chronically congested — this is BASELINE, not just post-outage load.**

Decisive evidence: `GET /health` is a **no-op coroutine** (`return {"status":"ok","version":...}` — zero DB, zero I/O, zero await; `app/main.py:830`). Yet on the **control node www2** (load 9, docker-`healthy`, **0 DB-lock errors**) it takes **4.15–8.34s** across repeated samples. A trivial coroutine that cannot be scheduled for 4-8s means the **event loop is blocked by synchronous/CPU-bound work running on the loop thread** — present even at idle. On **www1** it is worse (container pinned at **cpu=306%** of a 4-core limit, post-outage): `/health` >10s → docker healthcheck fails → `unhealthy`.

### ROOT CAUSE (verified 2026-08-07 via py-spy): aiosqlite connection-thread leak
**[FACT]** `py-spy dump` on the degraded node (www2, 7-day uptime) shows the process saturated with **`_connection_worker_thread (aiosqlite/core.py:63)`** threads: **232 OS threads** vs **38 on a freshly-restarted node (www1)** — ~190 leaked.
**Mechanism:** aiosqlite runs **one dedicated OS thread per DB connection**. The engine is configured `pool_size=50, max_overflow=100` (**up to 150 connections/threads**) + `pool_recycle=1800` + `pool_pre_ping=True` (`app/models/database.py:15-36`). This aggressively **churns** connections; when a teardown fails under contention (logs show `sqlalchemy.pool ... Exception terminating connection` and GC "non-checked-in connection ... will be terminated"), the aiosqlite worker thread **leaks permanently**. Over days → 190+ leaked threads → GIL/scheduler thrash burns ~1.5–3 cores **continuously** → the single asyncio event loop is starved → even the **no-op** `/health` (`app/main.py:830`, returns a dict) takes 4-8s (www2) or >10s (www1) → docker healthcheck fails → `unhealthy`. **Restart drops threads to 38 → `/health` back to 0.02s** (verified on www1).
**[FACT] The pool sizing is itself a misdiagnosis artifact.** Code comments (`database.py:19-31`) show the pool was escalated 15→50→150 and `pool_recycle` added "in case there's a slow leak we haven't found yet." Both changes **amplify** the thread leak. The comment "SQLite handles this fine… no network overhead per connection" ignores that each connection is an **OS thread**. SQLite is single-writer; it needs ~5–10 connections, not 150.

**Two symptoms, one cause + one aggravator:**
1. **[FACT] "database is locked" storm = LOAD-TRANSIENT aggravator.** 98 errors/5min at host-load ~50; **0/5min** at load ~25. Contributes teardown failures that leak threads, but self-resolves.
2. **[FACT] "unhealthy / slow no-op /health" = the aiosqlite thread leak above.** This is the real defect. NOT a DB-pool/session leak (SIGUSR2 dump: `async_sessions=0`), NOT memory (674MiB), NOT big-table scans (largest table 5,502 rows).

**Current status:** www1 restarted → `healthy`, 38 threads, /health 0.02s. **www2 still degraded** (232 threads, ~150% CPU) — left running as evidence; needs the same restart for relief. Both will re-degrade over days until the pool is resized + thread teardown fixed.

**Remediation direction (for Phase 5):** shrink pool to SQLite-appropriate (~`pool_size=5, max_overflow=10`); drop or greatly lengthen `pool_recycle`; verify aiosqlite threads terminate on connection close (or adopt a single-writer/`NullPool`+semaphore model); add **thread-count + event-loop-lag monitoring** and an interim thread-count-triggered recycle. Expected effect: thread count stays ~flat, `/health` stays sub-100ms, no periodic restarts needed.

---

### ✅ RESOLVED — v5.22.0 (2026-08-07), shipped both nodes
**Fix (`app/models/database.py`):** `pool_recycle=1800 → -1` (disable churn), `pool_size=50→40`, `max_overflow=100→10` (cap 50 = historically-workable). `pool_pre_ping` kept. **Plus:** `pool_leak_watcher` now monitors **OS-thread count** (warn 120 / crit 200 — the true leak metric the pool-slot watcher never saw); `/api…/metrics` exposes `runtime.threads` + `db_pool`; regression test `test_v5220_sqlite_pool_no_churn.py` pins the pool so the "bump it" reflex can't return.
**Verification (measured):**
- Config live on both nodes; `/health` 0.02–0.04s; fresh thread count 8–9.
- **Churn stress test** (8 rounds × 50 concurrent sessions = repeated overflow create/destroy): thread count `1 → 41` after round 1 and **flat at 41 across all 8 rounds** — overflow connections created and **reclaimed** each round, zero growth. The monotonic climb to 232 is eliminated on the churn path.
- Commit `3250fe4`, pushed `origin/v2`.
**Acceptance criterion (residual soak — the honest final proof):** on a node running real traffic, **OS-thread count stays < ~60 over ≥72h** (was climbing to 232 over 7 days). Now observable via `runtime.threads` / watcher warn@120. If it still creeps, the residual source is connections GC-orphaned on mid-`__aexit__` cancellation despite the `get_db` shield — next lever is to stop holding a DB session across the upstream call (larger refactor), not more connections.

---

## 1. Intended end state & primary users (provisional)
**[INFER]** A single controllable LLM gateway that routes traffic across many providers (claude-oauth/Anthropic-Max, cursor-oauth, codex/ChatGPT, grok-web bridge, cohere, OpenRouter, Vertex, Bedrock) via LMRH hints + capability/circuit-breaker/**compliance** gating, with cluster sync across nodes and OAuth session maintenance.
**Primary users:** (a) internal TMR dev/tooling (DevinGPT, coordinator hub, paperless, etc.); (b) **downstream gov-compliance consumers** who pull the Docker Hub image `dblagbro/llm-proxy-manager` and rely on the `app/compliance/` enforcement subsystem.
**[OPEN]** No single authoritative "intended end state / acceptance criteria" doc found; `architecture.md` + `docs/5.0-*` describe compliance but not overall done-ness. → deliverable #2 will define this.

## 2. Architecture & major execution paths
**[FACT]** FastAPI/Starlette, Python 3.13, single-process async (uvicorn). Datastore: **SQLite via aiosqlite + SQLAlchemy async** (`/app/data/llmproxy.db`, WAL, busy_timeout=5000, synchronous=FULL). Served at sub-path `/llm-proxy2/`.
**[FACT]** Sidecars: `llm-proxy2-grok-bridge` (Playwright/Chromium; **only on www1**), `llm-proxy2-cursor-bridge`.
**[FACT]** Request path: `/v1/chat/completions` & `/v1/messages` → hint parse (LMRH) → `select_provider` (enabled → external-rotation skip → node-local-session filter → compliance policy filter → tenant-ownership filter → circuit-breaker filter → family filter → capability filter → LMRH scoring) → provider dispatch (litellm or bespoke: grok-web, cursor bridge, claude-oauth chain) → cross-family translation.
**[FACT]** Always-on background writers (each writes SQLite): cluster_heartbeat, cluster_peer_refresh, cluster_sync_403_monitor, observability_sampler, usage_rotator, tool_capability_prober, keepalive, empty_success_burst_trigger, pool_leak_watcher. **This many concurrent writers on one SQLite file is the lock-contention source.**
**[FACT]** Cluster sync propagates providers/api-keys/settings via HMAC + LWW; **`extra_config` is synced** (so per-node values like `bridge_url` must be identical cluster-wide).

## 3. Implemented capabilities (with evidence)
| Capability | Evidence | Verdict |
|---|---|---|
| Multi-provider routing + LMRH hints | `app/routing/router.py`, `lmrh/` | **[FACT] works** (grok pin routed correctly) |
| Circuit breakers (in-memory, per-process) | `app/routing/circuit_breaker.py` | **[FACT] works**; note: per-process, invisible to subprocess introspection |
| grok-web bridge | rewritten v1.1.0 this session | **[FACT] works** end-to-end (9.9s 'Paris'); DOM-scrape |
| Vision hard-fail 422 | `router.py` v5.21.16 | **[FACT] works** (verified this session) |
| Empty-completion guards | `_messages_dispatch.py`, `messages.py` | **[FACT] shipped**; unit evidence [OPEN] |
| Compliance enforcement (`app/compliance/`) | `docs/5.0-*`, 3 tables | **[OPEN] not re-verified** — critical for downstream |
| Cluster sync | `app/cluster/` | **[FACT] runs**, but currently lock-contending |
| fd ulimit hardening (65536) | compose, both nodes | **[FACT] live**, survived reboots |

## 4. Partially implemented / abandoned / masked
- **[FACT] Disconnect-watchdog DISABLED fleet-wide** (`DISCONNECT_WATCHDOG_ENABLED=false`). Its job (cancel handlers on client disconnect to release DB connections) is unaddressed; mitigations = `pool_leak_watcher` self-heal + fd bump. **Proper fix (receive-stream tee) never done.**
- **[FACT] 9 test files untracked** (`tests/**/test_v5210…v5219…`) — tests for shipped v5.21.0–9 features exist on disk but were **never committed**; CI cannot see them; at risk of loss.
- **[INFER] grok-bridge fix not in the Docker Hub image** — the bridge builds locally from `grok_bridge/`; downstream image consumers don't get it (only www1 runs a bridge, so low impact, but a divergence).
- **[OPEN] cohere chat routing** — embeddings work; chat (command-r) deferred.

## 5. Tests: exist / fail / missing
- **[FACT] 328 unit test files + 15 integration files.**
- **[FACT] CI existed only since 2026-08-05** (commits `3c9a01c`, `9208560`, `c41b380`). Repo had **no CI before that**.
- **[FACT] Only blocking CI gate = 4 watchdog tests + `import app.main`.** Full unit suite runs **non-gating** (`continue-on-error: true`, `pytest … || true`).
- **[FACT] `tests/known_failures.txt` documents 64 committed failing unit tests** (categories: [SIZE] refactor-size asserts, [VER] stale version pins, [WIRE] drifted wiring greps, [BEHAV] behavioral). Candid note: "a repo that has never had CI."
- **[INFER] Test suite is not a reliable safety net** — 64 known-red, most of suite non-gating, 9 tests uncommitted.
- **[OPEN] Missing coverage:** DB-lock/concurrency behavior, compliance enforcement E2E, provider-dispatch integration under load.

## 6. Doc vs implementation conflicts
- **[FACT] `CLAUDE.md` says "Current state (2026-06-04): Live version v5.0.15"** — actual is **v5.21.16** (~2 months / many minor versions stale). Open-items list there is also stale.
- **[INFER]** `architecture.md` likely lags v5.21.x (flagged, not yet diffed).

## 7. Live risks (ranked)
1. **[FACT] P1 — SQLite "database is locked" storm → both nodes unhealthy.** 98 errors/5min, ~9 background writers. Architectural ceiling + post-outage load.
2. **[FACT] P1 — Core pool-leak bug unresolved**, watchdog disabled; only mitigated.
3. **[FACT] P2 — Test safety net weak** (64 known-red, non-gating suite, 9 uncommitted tests) → regressions can ship undetected.
4. **[INFER] P2 — Compliance subsystem unverified** while downstream depends on it.
5. **[FACT] P3 — Doc drift** (CLAUDE.md/architecture.md) → future agents act on false state.
6. **[INFER] P3 — Deploy provenance**: TMR runs locally-built images; relationship to Docker Hub tag/digest not continuously verified.

## 8. External dependencies / integration risks
**[FACT]** litellm (provider dispatch), Playwright/Chromium (grok bridge — fragile vs grok.com UI changes, proven this session), aiosqlite/SQLAlchemy async, bcrypt (passlib banned on 3.13), FastAPI/Starlette. **[INFER]** grok bridge is the most fragile external coupling (anti-bot cat-and-mouse).

## 9. Unresolved / under-validated decisions
- SQLite as datastore under this write load (see P1) — never validated at current concurrency.
- Disconnect-watchdog design (disabled, not redesigned).
- Many always-on background writer loops — necessity vs cost never audited.
- bridge single-instance on www1 vs per-node.

---

### Stage-1 remaining uncertainties
- Exact compliance-subsystem status (untested this session).
- Whether the 9 uncommitted tests pass (not yet run).
- architecture.md drift extent (not yet diffed).
- Whether DB-lock storm subsides as load normalizes, or persists at baseline load (needs a low-load recheck).

### Next verification steps (Stage 1 → close)
1. Recheck DB-lock frequency at normalized load (is it load-only or baseline?).
2. Run the 9 uncommitted tests + a sample of `known_failures` to confirm categories.
3. Spot-verify compliance enforcement path exists + has tests.
