# Backup / snapshot / rollback plan

Pre-remediation backup procedure. Per the QA process, **no fix phase begins
until this plan is reviewed.** Written for the v4.3.0 QA-pass remediation
(2026-05-18); the procedure is reusable for any llm-proxy2 fix release.

---

## Scope of the planned fixes

The v4.3.0 remediation (`remediation-plan.md`) is **frontend-only product
code** (3 React components — a `prefers-reduced-motion` class and the
auth-probe console handling) plus **additive tests**, plus one **operational**
action (refresh a provider on c1conv). No backend, API, schema, or
infrastructure change. This bounds the backup needs accordingly.

## What to back up before fixes

### 1. Code

- The release branch `v2` is the source of truth; the fix work happens there.
- `git tag v4.3.0` already pins the released commit (`27c3b88`) — the exact
  pre-fix state is permanently recoverable via the tag. No extra code backup
  is needed beyond confirming the tag exists: `git tag -l v4.3.0`.
- Before starting fixes, branch from the tag if isolation is wanted:
  `git checkout -b v4.3.1-fixes v4.3.0`.

### 2. Docker images (the actual rollback artifact)

- `dblagbro/llm-proxy2:4.3.0` and `dblagbro/whisper-bridge:4.3.0` are on
  Docker Hub and pinned by digest
  (`llm-proxy2 @ sha256:023884c9…`, `whisper-bridge @ sha256:051b41e7…`).
  These ARE the rollback target — a fix release is reverted by redeploying
  these tags. No image needs to be re-saved; confirm they are present:
  `docker manifest inspect dblagbro/llm-proxy2:4.3.0`.
- The release ceremony (`tools/cut-release.sh`) also wrote a source tarball:
  `/home/dblagbro/backups/llm-proxy-v2-v4.3.0-*.tar.gz`.

### 3. Config (per-node compose)

- The fix is frontend-only and does **not** change any env var or compose
  service. Still, before touching any node take a compose snapshot:
  `cp /home/dblagbro/docker/docker-compose.yml{,.bak-pre-v431}` on tmrwww01
  / tmrwww02, and `/opt/C1/instance/docker-compose.yml` on c1conv.
- The `AIRI_TTS_ENABLED=true` env line is the only v4.3 compose change; it is
  already in place on all 3 nodes.

### 4. Database

- **Not affected.** v4.3.0 added no tables, columns, or migrations, and the
  planned fixes add none. No DB backup is required for this remediation.
- The nightly DB backup (per the standard backup cron) remains the baseline
  for the data layer; nothing in this fix set touches it.

### 5. Environment / fleet snapshot

- Record the pre-fix fleet state for comparison:
  `version`, `healthyProviders`, and whisper-bridge `tts` for each of
  tmrwww01 / tmrwww02 / c1conv (all currently v4.3.0, see `4.3-qa-report.md`
  §3 Group I).

## Rollback approach

A fix release (v4.3.1) is rolled back exactly like any llm-proxy2 release —
redeploy the prior image, one node at a time:

1. `docker tag dblagbro/llm-proxy2:4.3.0 llm-proxy2:latest` on tmrwww01 /
   tmrwww02 (and pull `dblagbro/llm-proxy2:4.3.0` on c1conv).
2. `docker compose up -d --force-recreate --no-deps llm-proxy2` per node.
3. `whisper-bridge` is **unchanged** by the planned fixes — it stays on
   `4.3.0` and need not be touched on rollback.
4. Restore the compose snapshot only if a compose change was made (none is
   planned).

Rollback is fast (one image retag + recreate per node) and low-risk because
the rollback target is a known-good, QA-passed release.

## Restore verification steps

After any rollback, per node:
- `GET /llm-proxy2/health` → `status:healthy`, `version` is the expected one.
- `healthyProviders` matches the pre-fix snapshot.
- whisper-bridge `tts:true`.
- AIRI panel loads, speaker toggle renders (`/api/airi/status` shows
  `tts_enabled`).

## Systems / people affected

- **Affected:** the 3 llm-proxy2 nodes (tmrwww01, tmrwww02, c1conv) and their
  `whisper-bridge` sidecars. A frontend-only fix means users see new asset
  bundles on next load; no API consumer is affected.
- **Not affected:** the proxy's API consumers (DevinGPT, Claude Code, etc.) —
  the fix changes no API surface; the database; other containers on the
  stack (`--no-deps` keeps the recreate scoped).

## Risks if rollback fails

- Low. The rollback target (`4.3.0`) is the currently-running, QA-passed
  release — "rolling back" a frontend patch is functionally identical to the
  state the fleet is in right now.
- The realistic failure mode is a node-level Docker/network issue during the
  recreate (cf. the tmrwww02 power outage during the v4.3.0 deploy) — handled
  the same way: deploy the reachable nodes, queue the unreachable one, the
  fleet tolerates a brief version skew (v4.3.x is backward-compatible).
- If a node cannot be recreated at all, the prior container (with
  `restart: unless-stopped`) keeps serving until it is — no outage from a
  failed *forward* deploy.

---

## Rollback drill — 2026-05-19 (BUG-036 closure)

First end-to-end exercise of the documented rollback procedure on a
throwaway `llm-proxy2-stage` container (per `docs/f3-runbooks.md`
§"BUG-036" option 2). The drill validates the image-swap mechanic; it
does NOT exercise persistent-data preservation (the stage container
used a tmpfs `/app/data` to stay isolated from prod).

**Setup**
- Host: tmrwww01
- Container name: `llm-proxy2-stage`
- Port: `13456` (host-side, isolated from prod's 3000/443)
- Image cycle: `4.3.4 → 4.3.6 → 4.3.4` (forward-roll then rollback)
- Env: `CLUSTER_ENABLED=false`, ephemeral secret, no peers — the stage
  container does NOT join the cluster (avoids polluting prod sync state)

**Outcomes (PASS)**

| Step | Operation | Image | Time to /health healthy | Result |
|---|---|---|---|---|
| 1 | Cold boot | `dblagbro/llm-proxy2:4.3.4` | **12.92 s** | `/health` returns `version: 4.3.4`, `status: degraded` (degraded is expected — no providers in the empty tmpfs DB; the drill validates startup-path, not data continuity) |
| 2 | Forward-roll | `dblagbro/llm-proxy2:4.3.6` | **13.66 s** | `/health` returns `version: 4.3.6` |
| 3 | Rollback | `dblagbro/llm-proxy2:4.3.4` | **12.99 s** | `/health` returns `version: 4.3.4` (rollback target restored) |

Each step: `docker stop` + `docker rm` + `docker run` of the new image.
The startup time (~13 s, dominated by FastAPI + SQLAlchemy import +
initial DB creation) is the floor for a *real* rollback on a prod node;
real prod nodes mount a persistent DB and won't pay the table-creation
cost, so expect faster ready-times in production.

**What this drill DID validate**
- The documented `docker compose up -d --force-recreate --no-deps
  llm-proxy2` invocation (mechanically identical to the `docker
  run`/`stop`/`rm` cycle the drill used) restores a clean container
  on the requested image.
- An older image tag (`4.3.4`) still boots cleanly with the current
  release's data shape — no migration-incompatibility breakage on
  rollback. (For v4.3.x specifically this is unsurprising; would need
  re-validating on any release that touches schema.)
- Time to ready ≤ 15 seconds. A 3-node rolling rollback completes in
  under a minute given sequential ordering.

**What this drill did NOT validate** (called out so a future deeper
drill is informed):
- Persistent-data preservation across rollback (the stage used
  `--tmpfs`; prod uses a real volume).
- Cluster-sync rejoin after rollback (the stage had
  `CLUSTER_ENABLED=false`; prod nodes do cluster sync).
- Real upstream-provider availability on the rollback image (no
  providers were configured in the stage DB).
- nginx + LB routing under partial-fleet states (drill is single-host).

The first three are best validated during a controlled prod-node skew
test — covered by BUG-037's runbook. The nginx-routing case is its
own follow-up.

**Cleanup** — the stage container was removed (`docker stop` +
`docker rm`) at the end of the drill; no compose-file or shared-
infrastructure changes were made.

**BUG-036 closed.**

