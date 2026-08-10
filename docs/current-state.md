# Current state — llm-proxy-v2

> Brief live status. Keep it short and current. Detail lives in `architecture.md`,
> `docs/recovery/`, `CHANGELOG.md`. Last updated: 2026-08-09.

## Stage & objective
**Production, in active rescue.** Objective: stop the recurring node degradation, then resume the
staged recovery plan (`docs/recovery/01-current-state-assessment.md`, `docs/roadmap.md`).

## Branch / version
- Branch `v2`; last commit `3460d26` (**v5.22.3**). `origin/v2` in sync.
- Live on tmrwww01 + tmrwww02. Canonical deploy stack: `/home/dblagbro/docker/` (not the repo).

## What works (verified)
- Multi-provider routing + LMRH; vision hard-fail 422; empty-completion guards.
- **grok-web bridge** end-to-end (bridge v1.1.0, DOM-scrape) — verified.
- fd `nofile` ulimit hardening (65536) on both nodes.
- The unbounded aiosqlite **thread** leak (self-heal `engine.dispose()` thrash) is fixed —
  thread count no longer grows without bound.

## ✅ RESOLVED — DB-connection-hold leak (v5.22.4, option A)
Was: `/v1/messages` + `/v1/chat/completions` held the `get_db` session (an open read-txn from
provider selection) across the upstream call + entire stream, pinning an aiosqlite connection for
15-18 min → pool exhaustion → `/health` hangs → unhealthy every ~20 min. Same class as the
v5.21.6-8 SSE fixes (`runs.py`/`lmrh_v2.py`), now applied to the chat handlers.
**Fix (v5.22.4, commit `3fc664a`):** `await db.commit()` release-boundary after pre-dispatch reads
and before dispatch (both handlers); + commits after the dispatch-time re-selects that reopened a
txn across the stream (hedge, CoT-critique, empty-success failover, non-streaming apply_fanout);
pool raised 20→40 (holds are short now). `record_outcome`/`maybe_extract` commit internally so they
release on their own.
**Verified:** commit() releases the connection (checkedout 1→0, mechanic tested); 12 concurrent
streams → checkedout=0/40, 0 QueuePool errors; ~12 min realistic soak both nodes healthy,
checkedout=0 throughout, 0 QueuePool errors; independent adversarial review drove completeness; 0
new unit-test failures. `DB_POOL_TRACE` turned back OFF. Both nodes on v5.22.4.

## ✅ RESOLVED — aiosqlite OS-thread leak (v5.22.5, the deeper fix)
After v5.22.4, a node still reached **80 aiosqlite threads (2× pool cap) in 36 min under LIGHT
traffic** with recycle=-1 AND self-heal already off — proving those were only amplifiers. Root
cause: aiosqlite runs one OS thread per connection; any connection **created-then-destroyed
(overflow churn) or GC'd-not-closed (cancellation / pre_ping invalidation)** orphans its thread
forever.
**Fix (v5.22.5, commit `330f8bc`):** a **fixed, non-churning pool** — `max_overflow=0` (pool is
exactly `pool_size` connections, created once, reused forever, never destroyed → nothing to
orphan), `pool_pre_ping=False` (no invalidate/discard churn), `pool_size=50`, `recycle=-1`.
Structural guarantee: the pool can't create >50 connections and never destroys them.
**Verified:** under sustained load the thread count **plateaued at 36 (pool_size 30 test) and did
NOT climb toward 80**; `checkedout=0` throughout. Both nodes on v5.22.5.

## Active risk (P2, separate & pre-existing — NOT a leak)
- **Single-event-loop CPU ceiling under extreme concurrency.** py-spy (2026-08-10): the loop goes
  CPU-bound constructing SQLAlchemy queries (`select_provider`'s `.where(...)`) under an *abusive*
  synthetic burst (4-6 concurrent `model:auto` streams fired repeatedly) → queries slow → the fixed
  pool exhausts → `/health` starves → unhealthy, and it drains slowly. **NOT triggered by real
  ~2/min traffic** — the control node (real organic traffic) stayed healthy across every soak all
  session — and NOT a connection/thread leak (`checkedout=0`, threads capped). This is an
  architectural throughput limit, not the leak. Future work (separate): multiple uvicorn workers,
  a `/health` that doesn't need a pooled connection, and caching/curtailing per-request
  provider-selection query building. Tracked in `docs/roadmap.md` M1(P2).

## Other known gaps
- CI is weak: only 4 gating tests; full suite non-gating; **64 known-fail tests**
  (`tests/known_failures.txt`); **9 test files uncommitted** (`git status`).
- Doc drift: `architecture.md` header says v5.21.8; `bug-log.md`/`refactor-log.md` exist at BOTH
  repo root and `docs/` (divergent). See `docs/agent-system.md` self-healing backlog.

## Latest verification
Both nodes restarted → healthy, inference 200 in ~1.5s (fresh). Will re-degrade without the leak fix.

## Next 3 actions
1. Get operator direction on the leak fix (A/B/C); if C, land the session-reaper to stop the 20-min cycle.
2. Implement the chosen fix with **live** before/after verification (not a subprocess test).
3. Resume the recovery roadmap (test-suite green-up, doc drift consolidation).

## Resume commands
- Orient: read `AGENTS.md` + this file; `git -C /home/dblagbro/llm-proxy-v2 status -sb`.
- Health: `sudo docker exec llm-proxy2 python3 -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://localhost:3000/health')).get('version'))"`
- Pool trace: `sudo docker kill --signal=SIGUSR2 llm-proxy2` then `docker logs --since 8s llm-proxy2`.
