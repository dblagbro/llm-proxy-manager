# Current state — llm-proxy-v2

> Brief live status. Keep it short and current. Detail lives in `architecture.md`,
> `docs/recovery/`, `CHANGELOG.md`. Last updated: 2026-08-09.

## Stage & objective
**Production, in active rescue.** Objective: stop the recurring node degradation, then resume the
staged recovery plan (`docs/recovery/01-current-state-assessment.md`, `docs/roadmap.md`).

## Branch / version
- Branch `v2`; last commit `2acc675` (**v5.22.5**). `origin/v2` in sync (no upstream tracking ref set;
  `git rev-parse origin/v2` == HEAD). Working tree clean.
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

## 🔴 INCIDENT 2026-08-10 — wrong-host purge on tmrwww01 (agent error, service restored)
An agent session ran a purge against `c1conversations-avaya-01-s23` believing it a separate
dropped node. That hostname resolves to `24.168.14.36` (public WAN) and **NAT-hairpins back to
tmrwww01**, so every "remote" command executed on the live production node.
**Damage:** `llm-proxy2` container removed (www.voipguru.org 502 outage); devingpt stack removed;
5 docker volumes deleted, incl. `docker_devingpt_data` (774M — chatgpt.db, workspaces, skills,
exports, generated_audio, images). **Restored:** compose + nginx rolled back from backups,
`llm-proxy2` recreated (v5.22.5 healthy, its own DB volume was never touched), nginx valid and
reloaded, all endpoints 200/401. **Not restored:** devingpt containers are down; its volume was
recreated and seeded from the newest backup (`devingpt-post-v274132-verified-20260703T012717Z.db`,
2026-07-02) — everything in that volume other than `devingpt.db` is unrecoverable.
**Prevention:** compare `hostname` AND `/etc/machine-id` against local values before any
destructive action on a "remote" host. `CLAUDE.md` topology corrected (2 dev nodes, not 3).

## ✅ ROOT CAUSE FOUND — `_next_route` infinite loop (v5.22.6, fix written, NOT deployed)
`app/routing/fallback.py::_next_route` excluded only ONE provider per
`select_provider` call and "progressed" by re-adding an id already in the exclusion set —
a no-op — so the seed never changed and the loop spun forever, each pass issuing ~2 DB
queries per provider. **One** `/v1/messages` request that hit a provider error was enough to
peg the event loop and drain the pool to 50/50 on an idle node.
**Fix:** pass the cumulative `exclude_provider_ids` set `select_provider` has accepted since
v5.7.13; defensive guard raises instead of looping. Pin:
`tests/unit/test_v5226_next_route_terminates.py` (verified failing pre-fix, 5 tests).
**Verified:** import OK, 219 routing tests pass, 0 new lint findings, 0 new unit-test failures.
**DEPLOYED 2026-08-10 ~21:2x EDT (operator-approved, both nodes at once).** tmrwww01 +
tmrwww02 both on **v5.22.6 healthy**, 0 QueuePool errors since deploy. Playwright-verified
end-to-end on both: login OK, 7/7 authenticated APIs return real payloads (providers 7,
keys 8, users 2, metrics, activity 100 rows, cluster, status-pages), and all 10 UI routes
render their data with no error state. GCP node deliberately NOT touched.
Watch for recurrence: a wedge would show as climbing `QueuePool limit` counts + `/health` 500.
**Retires the P2 "event-loop CPU ceiling" entry below** — the node was spinning, not saturated.

## 🔴 (SUPERSEDED by the above) pool wedge symptoms on v5.22.5 (observed 2026-08-10)
Both live nodes wedged; `/health` returns 500 with
`QueuePool limit of size 50 overflow 0 reached` (13.7k occurrences on tmrwww01 alone).
Evidence gathered this session:
- **Wedge is not load-driven.** ~1 inbound request per 2 min. tmrwww01 DB file mtime `08:00`,
  `-wal`/`-shm` mtime `08:24` — container started ~08:20, so it wrote for ~4 min after boot and has
  written **nothing for 9 h**. Not a slow degradation; a hard stop.
- **Not the thread leak.** Threads capped at 56 (= pool_size 50 + workers) — v5.22.5's fixed
  non-churning pool is holding. Nothing is growing.
- **All 50 connections busy, none progressing.** `docker stats` ~180-208 % CPU with the asyncio
  MainThread **idle** in `run_forever`; per-thread sampling shows ~490 ticks/10 s on MainThread and
  ~27 ticks/10 s on each of the 50 aiosqlite `_connection_worker_thread`s, all parked at
  `aiosqlite/core.py:63` (`result = function()`) — i.e. inside SQLite, spinning, not returning.
  Signature of a **SQLite lock deadlock**, not a CPU ceiling and not a connection leak.
- One MainThread sample caught `messages.py:1030 → try_ranked_non_streaming → _next_route →
  select_provider → _load_profile (router.py:188)` CPU-bound in SQLAlchemy query *construction*.
- **Onset not in logs**: docker's json log file has rotated — earliest retained entry is 12:11 UTC,
  well after the 08:24 wedge. 13.7k QueuePool tracebacks flushed the evidence window.
**Revises the P2 note below**: the "only under abusive synthetic burst" framing does not fit this
event — real traffic was ~0. Treat the P2 entry as an unproven hypothesis until re-tested.
**Do not restart before deciding** — a restart clears the only live evidence.

## Latest verification (superseded)
Both nodes restarted → healthy, inference 200 in ~1.5s (fresh). Will re-degrade without the leak fix.

## Next 3 actions
1. Get operator direction on the leak fix (A/B/C); if C, land the session-reaper to stop the 20-min cycle.
2. Implement the chosen fix with **live** before/after verification (not a subprocess test).
3. Resume the recovery roadmap (test-suite green-up, doc drift consolidation).

## Resume commands
- Orient: read `AGENTS.md` + this file; `git -C /home/dblagbro/llm-proxy-v2 status -sb`.
- Health: `sudo docker exec llm-proxy2 python3 -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://localhost:3000/health')).get('version'))"`
- Pool trace: `sudo docker kill --signal=SIGUSR2 llm-proxy2` then `docker logs --since 8s llm-proxy2`.
