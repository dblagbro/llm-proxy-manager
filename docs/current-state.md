# Current state — llm-proxy-v2

> Brief live status. Keep it short and current. Detail lives in `architecture.md`,
> `docs/recovery/`, `CHANGELOG.md`. Last updated: 2026-08-13 (verified against live nodes).

## Stage & objective
**Production, stable.** The recurring node degradation is root-caused and fixed (v5.22.6); both
nodes have run clean since. Objective now: resume the recovery roadmap (test-suite green-up, doc
drift consolidation) and close the release-hygiene gaps listed under "Squaring up" below.

## Branch / version
- Branch `v2`; HEAD `0205f6d` (**v5.22.11**). `origin/v2` == HEAD (0 ahead / 0 behind).
  Working tree clean.
- **Both live nodes serve v5.22.11** and match HEAD's version pin.
- Canonical deploy stack: `/home/dblagbro/docker/` (not the repo).
- **GCP is out of scope** — the operator's strict-separation rule stands (reaffirmed 2026-08-13).
  No GCP node is part of this cluster, is deployed to, or is diagnosed from here.

## Working directories (MOVED 2026-08-13 — read this before building)
Source of truth moved off the home directory onto the `/mnt/s` NFS share (`192.168.18.5:/disk0`),
which both nodes mount, so there is now one shared tree.

| Path | Role |
|---|---|
| `/mnt/s/code/llm-proxy-v2` | **Source of truth.** Git worktree of `/mnt/s/code/llm-proxy`, branch `main`. Edit here only. |
| `/home/dblagbro/docker/build/llm-proxy-v2` | tmrwww01 build staging. Disposable rsync; overwritten by `stage-build.sh`. Never edit. |
| `/home/dblagbro/llm-proxy-v2` | **Pre-move copy — do not use.** tmrwww01: `.git` points at a worktree registered elsewhere. tmrwww02: pointer is dangling → orphaned non-git copy. |

Build on tmrwww01: `/home/dblagbro/docker/scripts/stage-build.sh llm-proxy-v2 --build`
(rsync SAN → local disk, then compose build). Building without staging first builds stale source.
Docker reads the entire ~305 MB context per build; over NFS that is slow and puts a hard-mounted
NFS path in the build's critical path — hence the staging tree.

**Verified 2026-08-13:** SAN source, tmrwww01 staging, tmrwww01 pre-move copy, and tmrwww02's
build tree are all content-identical (sha `f4ea143f1ca464e7` over `app/ frontend/src/ grok_bridge/
Dockerfile requirements.txt`), all pinned v5.22.11. Nothing has drifted *yet*.

## What works (verified live 2026-08-13)
- `tmrwww01` — `/health` 200, `healthy`, v5.22.11, node `llm-proxy2-www1`, **6/7 providers healthy**.
- `tmrwww02` — `/health` 200, `healthy`, v5.22.11, node `llm-proxy2-www2`, **7/7 providers healthy**.
- No pool wedge: DB file actively written (mtime current), no `QueuePool limit` errors — the
  v5.22.6 fix is holding. This is the signature to watch; a wedge shows as climbing
  `QueuePool limit` counts + `/health` 500.
- Multi-provider routing + LMRH; vision hard-fail 422; empty-completion guards.
- fd `nofile` ulimit hardening (65536) on both nodes.
- v5.22.7–v5.22.11 shipped and are live: password reset, Google/OIDC SSO, sign-in by email,
  `users.email` cluster replication, grok-bridge noVNC sub-path + honest `healthz`, Cohere
  tool-shape fix.

## ✅ 2026-08-15 — release hygiene restored; v5.22.11 shipped
The 12-version release gap is closed. `v5.22.11` is tagged, released on GitHub, and published
to Docker Hub as `dblagbro/llm-proxy2:5.22.11` + `:latest` (manifest verified pullable from
tmrwww02); release tarball at
`/mnt/s/tmrwww01-home-backups/backups/llm-proxy-v2-v5.22.11-20260815T183644Z.tar.gz`.

**Canonical Hub repo is `dblagbro/llm-proxy2`** (operator decision, 2026-08-15).
`dblagbro/llm-proxy-manager` is the **v1** image repo — see the naming cleanup below.

Only v5.22.11 was tagged; v5.22.0–v5.22.10 remain untagged by decision (their changes are all
contained in .11, which is what runs in production).

Fresh host-side DB snapshots taken before the cut, integrity-checked `ok`, stored off-volume:
`/home/dblagbro/backups/llmproxy.www{1,2}.pre-v52211.20260815T1834*.bak` (22 MB / 21 MB).

## 🔤 Naming cleanup — pending operator decision
The name is overloaded three ways and the "v2" suffix now describes a product that has no v1:
- **GitHub repo `dblagbro/llm-proxy-manager`** holds both: `main`/`master` = retired v1 (Node.js),
  `v2` = the current Python rewrite. Default branch is `v2`.
- **Docker Hub `dblagbro/llm-proxy-manager`** = v1 images (`5.8.3` is the running zombie), but v2
  releases were also pushed here (`5.21.16`, 2026-08-06). **Docker Hub `dblagbro/llm-proxy2`** is
  v2's real channel.
- **v1 zombies still running with no nginx route:** `llm-proxy-manager` (tmrwww01) and `llm-proxy`
  (tmrwww02). Every `/llmProxy/` location is commented `# v1 retired 2026-04-30`, and
  `$llm_proxy_upstream` was repointed to `llm-proxy2:3000` in the 2026-08-05 breach response.
  They serve nothing — safe to retire.

## 🟡 Structural gap — grok-web cannot work on tmrwww02
The `grok-web` provider is cluster-synced to both nodes, but the `llm-proxy2-grok-bridge` sidecar
exists **only on tmrwww01**. `Grok-Web-Devin` has an empty `base_url` and resolves the bridge by
docker service name, which does not exist on tmrwww02 — so the provider fails there permanently
and its breaker sits open (observed 2026-08-15: open, 8 failures, 7 opens). This is why the two
nodes report different `healthyProviders` counts. Fix is either a bridge sidecar on tmrwww02 or a
per-node exclusion for the provider. Not yet decided.

## 🟡 Degraded — grok-web path on tmrwww01 (RESOLVED 2026-08-15, kept for context)
- `llm-proxy2-grok-bridge` container is **unhealthy, failing streak 4203 (~35 h)**. Chromium is
  crashing: `cookie-refresh failed: Page.goto: Page crashed` navigating to `https://grok.com/`,
  repeating every 25 min. The container's supervisord is up — v5.22.8's *honest* `healthz` is
  correctly reporting the dead browser inside (this is exactly the BUG-025 class it was written
  to catch, working as intended).
- Consequence: provider **`Grok-Web-Devin`** (`8beb17c4bd11de26`) breaker is **half-open**,
  7 failures / 6 consecutive opens.
- **Resolved 2026-08-15** by recreating the single container (`up -d --force-recreate --no-deps
  llm-proxy2-grok-bridge`). Playwright restored its session from `llm-proxy2-grok-bridge-data`
  (`playwright ready; bridge listening`) — no operator re-login was needed. Container healthy;
  tmrwww01 back to 7/7.

## 🟡 Watch — Codex provider on tmrwww01 (self-recovered, probationary)
Provider **`Devin-Codex-Gmail`** (`c549ed05a1cd86d3`, type `ChatGPT-oauth-plan`) was **open** with
15 failures / 14 consecutive opens / ~64 min hold-down. By 2026-08-15 it had recovered on its own
to **half-open, 2 failures, no hold-down** — so this was transient upstream trouble, not a dead
OAuth session. Left alone deliberately; if it re-opens and stays open, re-auth the credential.

## Squaring up — release-hygiene gaps (open)
1. **Nothing since v5.21.16 was released — the operator-locked "every version bump = tag +
   GitHub release + Docker Hub push, same session" rule has been broken for 12 versions.**
   - GitHub: latest release **v5.21.16** (2026-08-06); newest local tag is also `v5.21.16`.
     **v5.22.0 … v5.22.11 have no tag and no release** — no pinned rollback point for the code
     currently in production.
   - Docker Hub is split across **two** repos and it is not clear which is canonical:
     `dblagbro/llm-proxy2` (what `tools/cut-release.sh` pushes) is at `:latest` = **5.21.9**
     (2026-07-16); `dblagbro/llm-proxy-manager` (what `AGENTS.md`/`CLAUDE.md` name as the
     distribution image) is at **5.21.16** (2026-08-06). **Operator decision needed.**
   - Either way, downstream consumers pulling `:latest` do not have the `_next_route`
     infinite-loop fix (v5.22.6) — the bug that wedged the nodes. Highest-value open item.
2. **`tools/cut-release.sh` cannot be run as-is** — it will fail or produce a wrong artifact:
   - Its pre-cut live-verify curls **three** canonical URLs, including the GCP node
     `c1conversations-avaya-01.avaya.c1cx.com`. That node is dropped/off-limits, so the check
     fails and **aborts every cut**. Needs the URL removed (not papered over with
     `--skip-live-verify`, which would also disable the two checks we *do* want).
   - Its backup-tarball step runs `tar -C /home/dblagbro llm-proxy-v2` — the **pre-move stale
     copy**. Must point at `/mnt/s/code/llm-proxy-v2`.
   - `DOCKER_REPO` is hardcoded to `dblagbro/llm-proxy2`; see the channel ambiguity above.
   - Its closing hints print `gcloud compute ssh …` redeploy commands for the GCP node.
   Fix the script **before** the next cut.
3. **Source-tarball backups are stale.** The `-backups` dirs at `/mnt/s/code/llm-proxy-v2-backups`
   and `/home/dblagbro/llm-proxy-v2-backups` are identical and stop at `v3.0.10` (2026-04-30);
   they are superseded by `cut-release.sh` step 10, which writes to
   `/mnt/s/tmrwww01-home-backups/backups/` — and hasn't run since v5.21.16. Code is covered by
   GitHub, so this is low risk; retire the two duplicate dirs.
4. **No host-side DB snapshots.** `/home/dblagbro/backups/` is **empty** — the
   `llmproxy.*.pre-*.bak` files that `docs/backup-plan.md` records as the standing safety net are
   gone. In-container snapshots exist but are stale and large: `llmproxy.db.v5.0.0.snapshot` and
   `llmproxy.db.v5.0.7.snapshot` (1.1 GB **each**, June) plus `llmproxy.db.bak-restore-20260721`,
   sitting inside the live `llm-proxy2-data` volume against a 22 MB live DB. That is ~2.2 GB of
   stale snapshot inside the volume it is supposed to protect — no off-volume copy. Take a fresh
   host-side snapshot and prune the two June ones.
5. **tmrwww02 was not migrated to the staging pattern.** Its compose still has
   `build: /home/dblagbro/llm-proxy-v2` — the orphaned, non-git pre-move copy. It is byte-identical
   to the SAN source today, but nothing keeps it in sync and, with no `.git`, drift is undetectable.
   Align it with tmrwww01 (`stage-build.sh` + `build: /home/dblagbro/docker/build/llm-proxy-v2`).
6. **Node images are built independently per node** (different image IDs: `a3114979f017` on www1,
   `e66fb6160b38` on www2), so deploys are not bit-identical. Acceptable under the current rolling
   model; publishing to Docker Hub and pulling would remove the class.

## Other known gaps
- CI is weak: only 4 gating tests; full suite non-gating; **64 known-fail tests**
  (`tests/known_failures.txt`). (The "9 test files uncommitted" note is resolved — tree is clean.)
- Doc drift: `architecture.md` header says v5.21.8; `bug-log.md`/`refactor-log.md` exist at BOTH
  repo root and `docs/` (divergent). See `docs/agent-system.md` self-healing backlog.
- Smoke instance (`llm-proxy2-smoke`) is on **v5.21.12**, 1/3 providers healthy — well behind the
  live nodes. Fine as an isolated sandbox, but stale for pre-promotion validation.

## Resolved — keep for context
- **`_next_route` infinite loop (v5.22.6).** `app/routing/fallback.py::_next_route` excluded only
  one provider per `select_provider` call and "progressed" by re-adding an id already excluded — a
  no-op — so the loop spun forever, ~2 DB queries per provider per pass. One `/v1/messages` request
  hitting a provider error pegged the event loop and drained the pool to 50/50 on an idle node.
  Fix: pass the cumulative `exclude_provider_ids` set; defensive guard raises instead of looping.
  Pin: `tests/unit/test_v5226_next_route_terminates.py`. **This retired the P2 "event-loop CPU
  ceiling" hypothesis — the node was spinning, not saturated.**
- **DB connection-hold leak (v5.22.4)** — chat handlers held `get_db`'s open read-txn across the
  upstream call and entire stream. Fix: `await db.commit()` release-boundary before dispatch.
- **aiosqlite OS-thread leak (v5.22.5)** — aiosqlite runs one OS thread per connection; churned or
  GC'd-not-closed connections orphan threads forever. Fix: fixed non-churning pool
  (`max_overflow=0`, `pool_pre_ping=False`, `pool_size=50`, `recycle=-1`).

## 🔴 INCIDENT 2026-08-10 — wrong-host purge on tmrwww01 (agent error, service restored)
An agent session ran a purge against `c1conversations-avaya-01-s23` believing it a separate dropped
node. That hostname resolves to `24.168.14.36` (public WAN) and **NAT-hairpins back to tmrwww01**,
so every "remote" command executed on the live production node.
**Damage:** `llm-proxy2` container removed (502 outage); devingpt stack removed; 5 docker volumes
deleted incl. `docker_devingpt_data` (774M). **Restored:** compose + nginx rolled back, `llm-proxy2`
recreated (its DB volume was never touched), all endpoints 200/401. **Not restored:** everything in
the devingpt volume other than `devingpt.db` is unrecoverable.
**Prevention (enforced):** compare `hostname` **and** `/etc/machine-id` against local values before
any destructive action on a "remote" host. Verified 2026-08-13 — tmrwww01 `297da5f1…`,
tmrwww02 `9cca36eb…`: genuinely distinct hosts.

## Next 3 actions
1. **Settle the naming cleanup** (see above) — retire the two v1 zombie containers, archive the v1
   branches, and decide whether the product keeps the `2`/`v2` suffix. Phase anything that changes
   the `/llm-proxy2/` nginx path or the Hub repo name; those break callers.
2. **Close the grok-web/tmrwww02 structural gap** — bridge sidecar on www2, or exclude the provider
   per-node so its breaker stops flapping.
3. **Align tmrwww02 onto `stage-build.sh`** (it still builds from the orphaned non-git copy), then
   prune the two 1.1 GB June snapshots out of the live volume now that host-side backups exist.

## Resume commands
- Orient: read `AGENTS.md` + this file; `git -C /mnt/s/code/llm-proxy-v2 status -sb`.
- Health (per node): `curl -s https://www.voipguru.org/llm-proxy2/health` and
  `https://www2.voipguru.org/llm-proxy2/health` — check `status`, `version`, `healthyProviders`.
- Stage + build (tmrwww01): `/home/dblagbro/docker/scripts/stage-build.sh llm-proxy-v2 --build`
- Deploy: `cd /home/dblagbro/docker && sudo docker compose up -d --force-recreate --no-deps llm-proxy2 && sudo docker exec nginx nginx -s reload`
- Pool trace: `sudo docker kill --signal=SIGUSR2 llm-proxy2` then `docker logs --since 8s llm-proxy2`.
