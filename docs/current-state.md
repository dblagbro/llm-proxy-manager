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

## What does NOT work / active risk (P1)
- **DB-connection-hold leak (unresolved).** `/v1/messages` (and other) requests acquire a
  `get_db` session and hold it for the *entire request*; some requests hang 15-18 min and never
  release (proven via `DB_POOL_TRACE` — `messages.py:103` / `cluster.py:65` sessions pinned).
  The pool fills, then requests 500 and `/health` hangs. **Nodes re-degrade ~every 20 min.**
  Pool-size tuning cannot fix this — it only changes the exhaustion ceiling.
  - **This is the same class as the documented v5.21.6-8 fix** ("DB pool leak diagnostic path" in
    `architecture.md`: `Depends(get_db)` + `StreamingResponse` holds the session for the whole
    stream). Those fixed specific SSE sites (`runs.py`, `lmrh_v2.py`); the `messages.py`/request
    path is the same pattern, unfixed.
  - **Pending decision (A/B/C):** A = release the DB session *before* the upstream call (scoped
    handler refactor — the durable fix); B = re-enable a *fixed* disconnect-watchdog (cancel on
    client disconnect); C = stopgap session-reaper (force-close `get_db` sessions held > ~3 min).
    Recommended: **A**, with **C** as an immediate stabilizer. Awaiting operator direction.
- `DB_POOL_TRACE=1` is currently ON on www1 (diagnostic) — turn back off with the chosen fix.

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
