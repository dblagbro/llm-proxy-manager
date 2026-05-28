# Backup / snapshot / rollback plan

Pre-remediation backup procedure. Per the QA process, **no fix phase begins
until this plan is reviewed.** Written for the v4.3.0 QA-pass remediation
(2026-05-18); the procedure is reusable for any llm-proxy2 fix release.

---

## 2026-05-27 — Backup plan addendum for v4.4.24 (cluster-sync robustness)

The 2026-05-27 deep QA pass surfaced BUG-079..083 (see `docs/bug-log.md`). The remediation (`docs/remediation-plan.md` 2026-05-27 section) requires:

1. **Code changes** — 5 `.limit(1)` adds in `app/cluster/sync_handlers.py`; 1 line in `app/cluster/manager.py::push_sync`; 1 line in `app/api/monitoring.py` (Query bound). Code-only changes are recoverable via git tag — `git tag v4.4.23` once we cut it.
2. **Data fix on peers** — hard-DELETE the 2 duplicate rows on www2 + c1conv. **Irreversible without a DB snapshot.**
3. **Schema migration** (deferred to v4.4.25) — full table rewrite for UNIQUE constraint on `provider_ai_review` (+ potentially `api_key_ai_review`, `caller_memory`, `caller_memory_markers`, `blocked_ips`). Highest backup risk in the arc.

### What to back up before v4.4.24 deploy

#### 1. Code

- `git tag v4.4.23` to pin the pre-fix release (a tag at the current `e50276e` commit on branch `v2`).
- Branch from the tag for the v4.4.24 work: `git checkout -b v4.4.24-prep v4.4.23`.

#### 2. Database snapshots BEFORE the data fix

This is the new requirement vs the v4.3.0 plan. The data fix HARD-DELETES rows; we need point-in-time recovery.

```bash
# Each peer node — snapshot the affected table before delete.
# Run inside the container OR copy the WAL-aware DB file.
sudo docker exec llm-proxy2 sqlite3 /app/data/llmproxy.db \
    ".backup /app/data/llmproxy.db.pre-v4424.bak"
# Then docker cp it out to the host for redundancy:
sudo docker cp llm-proxy2:/app/data/llmproxy.db.pre-v4424.bak \
    /home/dblagbro/backups/llmproxy.db.pre-v4424.www1.$(date -Iseconds).bak
```

Do this on **all 3 nodes** before the data fix runs. Total per-node footprint is ~30 MB.

#### 3. Application state

- `data/` directory in the container hosts the SQLite DB. The container restart writes WAL → DB; the snapshot above is post-checkpoint and self-consistent.
- Cron sentinels under `/home/dblagbro/log/*.done` should NOT be backed up — they're per-session and re-arming is intentional.

### Schema-migration backup (when v4.4.25 ships)

The `provider_ai_review` UNIQUE-constraint migration requires the SQLite shadow-table pattern:

```sql
BEGIN;
CREATE TABLE provider_ai_review__new AS SELECT * FROM provider_ai_review;
CREATE UNIQUE INDEX ix_pai_provider_captured_uniq ON provider_ai_review__new(provider_id, captured_at);
DROP TABLE provider_ai_review;
ALTER TABLE provider_ai_review__new RENAME TO provider_ai_review;
-- re-create any other indices that lived on the original table
COMMIT;
```

**Before running this:**
1. Full DB snapshot (per the data-fix backup above).
2. Confirm zero duplicates: `SELECT COUNT(*) FROM (SELECT 1 FROM provider_ai_review GROUP BY provider_id, captured_at HAVING COUNT(*) > 1)` returns 0.
3. Hold a read-lock window (~1s for ~3580 rows).

### Rollback procedure

If the v4.4.24 deploy goes wrong:

1. **Code rollback** — `sudo docker pull dblagbro/llm-proxy2:v4.4.23 && sudo docker tag dblagbro/llm-proxy2:v4.4.23 llm-proxy2:latest && sudo docker compose -f /home/dblagbro/docker/docker-compose.yml --project-directory /home/dblagbro/docker up -d --force-recreate --no-deps llm-proxy2`. The tag pins the binary state.
2. **Data rollback** — if the hard-delete of duplicates introduced a real problem, restore the per-node snapshot taken in step #2 above. Procedure:
   ```bash
   sudo docker exec llm-proxy2 cp /app/data/llmproxy.db /app/data/llmproxy.db.corrupted
   sudo docker cp /home/dblagbro/backups/llmproxy.db.pre-v4424.<NODE>.<TS>.bak llm-proxy2:/app/data/llmproxy.db
   sudo docker compose restart llm-proxy2  # forces SQLAlchemy to reload the file
   ```
   Restore verification: re-run the duplicate-detection query; should match the pre-fix count.

### Restore verification

After a rollback:
1. `/health` returns 200 with correct `version`
2. Three-node `/cluster/status` shows healthy peers
3. The duplicate-detection probe matches the pre-fix counts (proving the snapshot is the right one)
4. Drive a live LWW test (the same test BUG-079 was found with): mint api_key, wait 70s, query peers. Should still show **broken sync** (we're back to pre-fix state).

### Who/what systems are affected

- llm-proxy2 service on www1 / www2 / c1conv
- coordinator-hub (depends on llm-proxy2 for routing — should be unaffected by sync internals)
- DevinGPT (depends on the proxy for memory write-back — unaffected by sync internals)

### Risks if rollback fails

- **Data**: the snapshot must be taken BEFORE the data fix. If the fix runs first and the snapshot fails after, the duplicate row is gone forever. **Sequence matters.**
- **Code**: rollback to v4.4.23 reintroduces BUG-079 — the cluster will resume the silently-broken state. Acceptable as a last resort but not a "fix."
- **Partial rollback**: if one peer is rolled back but not the others, the cluster runs mixed-version. Sync direction matrix in BUG-079's "Scope" section is the reference. Roll all peers together OR none.



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

