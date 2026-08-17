# Current state — llm-proxy-v2

> Brief live status. Keep it short and current. Detail lives in `architecture.md`,
> `docs/recovery/`, `CHANGELOG.md`. Last updated: 2026-08-17 (verified against live nodes).

## Stage & objective
**Production, stable.** The recurring node degradation is root-caused and fixed (v5.22.6); both
nodes have run clean since. Objective now: resume the recovery roadmap (test-suite green-up, doc
drift consolidation) and close the release-hygiene gaps listed under "Squaring up" below.

## Branch / version
- **Repo: https://github.com/dblagbro/llm-proxy-manager** — branch **`main`** (renamed from `v2`
  on 2026-08-17 and now the default). `origin/main` == HEAD, 0 ahead / 0 behind, tree clean.
  Retired v1 lives on `archive/v1-main` / `archive/v1-master`.
- Version **v5.22.11** live; **v5.23.0** in flight on `feat/ollama-cold-load-defaults`
  (local Ollama timeout/native-tools/default-model defaults — first LAM slice).
  Tagged/released pin remains `0205f6d` until 5.23.0 ships.
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

## ✅ Naming cleanup — Tier 1 COMPLETE (2026-08-17)
Operator decisions: future product name is **`llm-proxy-new`**; scope is **Tier 1 only** (hygiene
that no caller can see). The name does not take effect anywhere yet — Tier 1 touches no image
repo, container name, or URL path. See the caveat at the end of this section.

**Done:**
- **v1 container `llm-proxy-manager` retired on tmrwww01** (stopped + removed, 2026-08-17). Verified
  first that no nginx route reached it and no *running* container referenced it. Its compose block
  is commented out with the rationale. **Volumes deliberately preserved:** `docker_llm-proxy-data`,
  `docker_llm-proxy-logs`, and host dirs `/opt/llm-proxy-data/*`. Rollback material:
  `/home/dblagbro/backups/v1-retirement/`.
- Branch references repointed to `main` (CI, `cut-release.sh`, skills, docs).

- **tmrwww02's `/llm-proxy/` clone RETIRED (2026-08-17, operator-approved).** It was *not* a v1
  zombie — it ran `llm-proxy2:latest` and was the operator-requested 2026-06-05 snapshot-and-fork
  (own DB, own cluster identity, `node=llm-proxy-www2`, v5.21.14). It was retired because it had
  **no external consumers**. Evidence gathered before removal, after an initial read of "3,234
  requests in 7 days" that looked like live demand:
  - every one of those `POST /v1/messages` ended **499** (client hung up), each firing ~10 s after
    the container's own `keepalive.swept` line on a ~10 m 20 s cadence — self-generated, not inbound;
  - `/proc/net/tcp` in its netns showed only the LISTEN socket, **zero established inbound
    connections**; its only outbound sockets were cluster-sync attempts to a peer deleted on
    2026-08-05, which 403'd forever;
  - the nginx access log had **zero** `/llm-proxy/` hits.

  nginx `$llm_proxy_upstream` on www2 was repointed to `llm-proxy2:3000` first, so legacy
  `/llm-proxy/` URLs still resolve — **verified 200 on both nodes after the change**. Volumes
  `docker_llm-proxy-data` / `docker_llm-proxy-logs` preserved; final DB snapshot (integrity `ok`)
  and config inspect in `/home/dblagbro/backups/v1-retirement/`.

  ⚠️ **Gotcha worth remembering:** `sed -i` on `nginx.conf` **replaces the file's inode**, and the
  nginx container bind-mounts that single file — so the running container kept serving the *old*
  content and `nginx -s reload` changed nothing (502s, `llm-proxy could not be resolved`). A
  `docker restart nginx` re-establishes the mount. Prefer in-place edits, or expect the restart.

- **Branch rename COMPLETE (2026-08-17).** Initially blocked by a GitHub partial outage (branch-rename
  API 503); reapplied once GitHub returned to All Systems Operational. Final state:
  | Before | After |
  |---|---|
  | `main` (retired v1, `47deb5f`) | `archive/v1-main` |
  | `master` (retired v1, `81968a9`) | `archive/v1-master` |
  | `v2` (current code, default) | **`main`** (default) |
  Local branches renamed to match; the `/mnt/s/code/llm-proxy` worktree now sits on
  `archive/v1-main` (still v1 code, unchanged) and `/mnt/s/code/llm-proxy-v2` on `main`, tracking
  `origin/main`, 0 ahead / 0 behind. CI's transitional `[ main, v2 ]` trigger is back to `[ main ]`.
  **Repo URL: https://github.com/dblagbro/llm-proxy-manager** (the repo itself is deliberately NOT
  renamed — that is Tier 2/3 work).

**Still open — the `llm-proxy-manager` Hub repo.** `dblagbro/llm-proxy-manager` on Docker Hub holds
a mix of v1 images (`5.8.3`) and v2 releases pushed there by mistake (`5.21.16`, 2026-08-06). The
canonical channel is `dblagbro/llm-proxy2`. Mark the other deprecated in its Hub description; no
API call is scripted for this yet (Docker Hub needs a PAT-issued JWT, not the docker CLI creds).

**Caveat on the chosen name.** `llm-proxy-new` carries the same defect as `llm-proxy2`: a relative
qualifier frozen into a permanent identifier. "new" dates the moment it is created, and the next
rewrite has nowhere to go. It costs nothing today because Tier 1 renames nothing — but it should be
settled before Tier 2/3, when it would be baked into a Hub repo, container names, and a URL path.

## ✅ CORRECTION — there is no grok-web/tmrwww02 structural gap
An earlier entry here claimed `grok-web` could never work on tmrwww02 because the
`llm-proxy2-grok-bridge` sidecar runs only on tmrwww01. **That was wrong**, and the reasoning
behind it (empty `base_url` ⇒ resolves the bridge by docker service name) was the wrong field:
`grok-web` takes its bridge address from `extra_config.bridge_url`, not `base_url`. The live value
on `Grok-Web-Devin` is **`https://www.voipguru.org/grok-bridge`** — the *public* URL, reachable
from both nodes, which is why the cluster-synced config is correct as-is.

What actually happened: the breaker on tmrwww02 was still in hold-down from the 35-hour Chromium
crash (see below). Once the bridge was recreated it recovered on its own. **Verified 2026-08-17:
tmrwww02 reports 7/7 providers, grok breaker half-open with zero hold-down.** No sidecar and no
per-node exclusion is needed. (Only the disabled `__grok_probe3__` row still carries the
internal-only `http://llm-proxy2-grok-bridge:8443`, which is fine — it is disabled.)

## 🔴 Needs operator action — Codex provider re-opened on tmrwww01
`Devin-Codex-Gmail` (`c549ed05a1cd86d3`, `ChatGPT-oauth-plan`) did **not** stay recovered. As of
2026-08-17 it is **open again: 19 failures, ~56 min hold-down** on tmrwww01 (half-open with 21
failures on tmrwww02). The 2026-08-15 "self-recovered, probationary" note is superseded — this is
a persistent failure, not a transient blip. It is the sole reason tmrwww01 reports 6/7.
**Re-auth the OAuth credential**; nothing in the routing layer will fix it.

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

## ✅ Backlog cleared 2026-08-17
- **Stale snapshots pruned.** `llm-proxy2-data` held two 1.1 GB June snapshots plus a July restore
  copy *inside* the live volume. Moved off-volume to `/home/dblagbro/backups/archive-snapshots/`
  (preserved, not deleted). **Volume: 2.2 GB → 26 MB.**
- **Duplicate logs merged.** `bug-log.md` and `refactor-log.md` each existed at the repo root *and*
  under `docs/` with **fully disjoint** contents — 10 vs 14 bug sections, 28 vs 4 refactor entries,
  zero overlap either way. Reading one showed half the history. Merged into the root files (nothing
  dropped); the `docs/` paths are now pointers. Root is canonical — `AGENTS.md` names it and
  `cut-release.sh` greps commits for `^bug-log\.md$`.
- **`architecture.md` caught up.** It stopped at the v5.21.x arc; added v5.22.x and marked the older
  v5.21.6–8 pool-leak section as superseded *as an explanation* (it described a symptom; v5.22.6's
  `_next_route` loop was the cause) while keeping its diagnostic method.
- **Smoke instance updated** from v5.21.12 → **v5.22.11**, now `healthy` with **3/3** providers
  (was 1/3) — usable again for downstream pre-promotion validation.
- **CI gating widened 4 → 8 test files.** Added the hermetic v5.22.x pins, most importantly
  `test_v5226_next_route_terminates.py` — the regression pin for the wedge root cause, which
  existed but **CI never ran it**. The v5.22.7/9/10 pins were deliberately left out: they pass
  locally but reach the live deployment via `conftest.py`, so they'd fail on a clean runner.

## Other known gaps
- **The real CI blocker is not the 75 `known_failures.txt` entries** — it is that
  `tests/conftest.py` session fixtures authenticate against the **live production deployment**.
  That makes much of the suite unrunnable on a clean runner *and* means running it locally mutates
  production (see the CAUTION in `docs/test-plan.md`). Making those fixtures hermetic is the single
  highest-leverage test-infra change available.
- **nginx logs on the NFS share are unrotated and large:** `/mnt/s/documents/access.log` **14 GB**
  and `error.log` **41 GB**, both still growing. Not llm-proxy2's doing (shared nginx), but they
  live on the share and no logrotate is trimming them.
- **tmrwww02's compose is broken for whole-stack operations** — `docker compose config` fails on a
  missing `/opt/secure-env/dumpthedump.env`. Pre-existing (the untouched pre-edit backup fails
  identically), and harmless for `--no-deps` single-container work, but `docker compose up` on that
  node would not work today.

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
1. **Land v5.23 local-accelerator slices** — slice 2 (cold-load defaults) first so a 30B Ollama
   load does not open the breaker; then the read-only VRAM/RAM/`ollama ps` probe; then admission
   429/503. Spec: `docs/5.23-local-accelerator-orchestration-backpressure-design.md`. Do not
   confuse with MCP capability-signalling back-pressure (`docs/5.10-mcp-backpressure-design.md`).
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
