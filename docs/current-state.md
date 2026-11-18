# ✅ Live now — 2026-11-18 (v5.22.64, both nodes)

**v5.22.64 deployed to tmrwww01 + tmrwww02.** Contents: PR #28 (merge `14322d0`), Grok
"Create new" shows a sign-in hint when the bridge is signed out. Rollback tag:
`llm-proxy2:rollback-5.22.63-pre64`. DB backups:
`/home/dblagbro/backups/llmproxy-www{1,2}-pre64-*.db`.

**Ops changes outside the repo (grok bridge):**
- Grok bridge compose now sets `BRIDGE_PUBLIC_ORIGIN=https://www.voipguru.org` and
  `BRIDGE_AUTH_URL`.
- nginx: `/grok-bridge/vnc/websockify` now proxies to the bridge app on 8443 (websockify
  itself is loopback-only), and `/grok-bridge/` redirects to `/grok-bridge/login`.

---

# ✅ Live now — 2026-11-18 (v5.22.63, both nodes)

**v5.22.63 deployed to tmrwww01 + tmrwww02** (rolling, tmrwww01 first; image copied
with `docker save | docker load`; each node healthy, 7/7, `/health` 5.22.63).
Contents: the August local-provider PRs #2/#3/#4 rebased via PR #26 (Ollama cold-load
defaults; read-only accelerator probe `GET /api/local/accelerators`, off by default;
admission 429/503 skeleton, not wired). Fixes found while testing: Dockerfile now
`chmod -R a+rX` on copied source (NFS-created `app/resources/` was 0774 and unreadable
by appuser), and `app/api/providers.py` split (schemas -> `_provider_schemas.py`) to stay
under the 800-LOC guard. Tested: CI green; full unit suite inside the py3.13 image
4209 passed / 4 environment-only failures (same 4 as v5.22.62).
Rollback tags: `llm-proxy2:rollback-5.22.62-pre63`. DB backups:
`/home/dblagbro/backups/llmproxy-www{1,2}-pre63-*.db`.

---

# ✅ Live now — 2026-11-18 (v5.22.62, both nodes)

**v5.22.62 deployed to tmrwww01 + tmrwww02** (rolling, tmrwww01 first; image built
on tmrwww01 and copied to tmrwww02 with `docker save | docker load`; each node
verified `/health` 5.22.62, healthy, 7/7 providers right after the recreate).
Contents: PR #23 provider "Needs attention" panel (`/api/provider-attention`,
admin-only, per-node) and PR #22 non-blocking Grok release snapshot storage warning.
Tested: CI green; inside the py3.13 image, focused tests 49 passed, full unit
suite 4154 passed / 4 failed (environment-only: read-only mount, host backup copy,
no playwright in the prod image); grok release tests 87 passed.
Rollback tags per node: `llm-proxy2:rollback-5.22.61-pre62`. Pre-deploy DB backups:
`/home/dblagbro/backups/llmproxy-www{1,2}-pre62-20261117T171747.db`.

**Also changed 2026-11-17 (ops, not code):**
- `USAGE_RESET_ADVISOR_ENABLED=true`, `USAGE_RESET_ADVISOR_FABLE_MIN_PP=20` in both
  nodes' compose (advisor worker reports ok).
- Grok bridge rebuilt from main (incl. PR #17) on tmrwww01; `.env` BRIDGE_TOKEN aligned
  to the token already in use (no rotation). Bridge needs an operator grok.com login.
- nginx (both nodes): off-LAN browsers hard-refreshing `/llm-proxy2/metrics` now get the
  SPA page instead of 403; Prometheus text stays LAN-only.
- tmrwww02 compose: dormant `llm-proxy2-smoke` service removed (volumes kept).

---

# ✅ Live now — 2026-11-13 (v5.22.61, both nodes)

**v5.22.61 deployed to tmrwww01 + tmrwww02** (rolling, peer-first; each node
verified `/health` version + healthy, RestartCount=0). Releases since the
2026-11-11 banner below: v5.22.57 (PR #16, per-key `metered_allowed` guard +
fallback error attribution), v5.22.60 (PR #18), v5.22.61 (PR #19). Rollback tags
per node: `llm-proxy2:rollback-5.22.57-pre60`, `llm-proxy2:rollback-5.22.60-pre61`.
Pre-deploy DB backups: `/home/dblagbro/backups/tmrwww0{1,2}-pre-5.22.60-20261113-215525.db`.

**What changed and what was verified live (2026-11-13/14 UTC):**
- **OpenAI spend-cap retry storm fixed.** The capped OpenAI project answers 429
  "enforced spend limit"; it was retried 8 times (~20 s) on every coordinator-hub
  `gpt-4o` request, ~2,700 `openai_client_retry` rows/day. v5.22.60 classifies it
  as billing (fail on first attempt); v5.22.61 opens the breaker when the failure
  is absorbed by a successful fallback. Soak on tmrwww01: first request after
  deploy made 2 SDK retries and opened the breaker (6 h hold); the next `gpt-4o`
  request went straight to Cohere with 0 retries. The breaker is in-memory per
  node: it re-probes after 6 h and re-trips until the cap is raised.
- **grok-web:** fallback no longer sends it through litellm to api.x.ai; probe
  timeout is 60 s for grok-web; provider-test rows no longer log "ok" on failure
  (all provider types). **Grok is still down:** the bridge is logged out of
  grok.com, and the RUNNING bridge image (built 2026-10-29) predates PRs #13/#14,
  so the merged bridge access-control and the new logged-out checks are not in
  force until the bridge is rebuilt. Rebuild + operator login pending.
- **Usage reset advisor** (see section below): shipped OFF
  (`USAGE_RESET_ADVISOR_ENABLED` unset). Advisory only; never redeems.
- **Per-key metered guard:** `tax-ai-analyzer` key has `metered_allowed=0`
  (subscription providers only). No real tax-ai request has exercised it: the app
  has sent only its 6-hourly `GET /v1/models` health check since 2026-11-09 11:58 UTC.

**Ops changes outside the repo (2026-11-13):**
- **nginx docs/metrics lockdown now actually in force.** `/llm-proxy2/{docs,redoc,
  openapi.json,metrics}` are allow-listed to private networks INLINE in
  `nginx.conf` on both nodes. The 2026-11-11 edit to
  `projects-locations.d/llm-proxy2.conf` never took effect: that directory is
  flattened into `nginx.conf` and no longer included. **Edit `nginx.conf`, not
  `projects-locations.d/`.** Not yet confirmed with a 403 from an outside address.
- **`llm-proxy2-smoke` torn down on tmrwww01** (compose service, nginx routes,
  container, image). Volumes `docker_llm-proxy2-smoke-data` / `-logs` kept.
  tmrwww02's compose file still carries a dormant smoke service definition.

**Provider fleet (2026-11-14 03:41 UTC):** 6 of 7 healthy; OpenAI held open by the
billing breaker (expected). OpenRouter key still invalid (priority 20, last
resort). Codex, both Max accounts and Cohere carry the load.

Older banners (2026-11-11 and earlier) moved to `docs/recovery/2026-history.md`.

---

# Current state: llm-proxy-v2

> Brief live status. Keep it short and current. Detail lives in `architecture.md`,
> `CHANGELOG.md` and `docs/recovery/`. Dated log material from before 2026-11-13 (older
> banners, the August to October state sections, incident and backlog notes) was moved
> verbatim to [`recovery/2026-history.md`](recovery/2026-history.md) on 2026-11-18.
> Last updated: 2026-11-18.

## Stage & objective
**Production, stable maintenance.** The node-degradation causes are fixed and have stayed
fixed: the DB connection-hold leak (v5.22.4), the aiosqlite thread leak (v5.22.5) and the
`_next_route` loop (v5.22.6). Details are in the history file under "Resolved".

## Branch / version
- **Repo: https://github.com/dblagbro/llm-proxy-manager**, default branch **`main`**.
- **v5.22.64 is live on tmrwww01 and tmrwww02** (2-node cluster; tmrwww01 primary, tmrwww02
  backup). It matches the `app/__version__.py` pin on `main`.
- Canonical deploy stack: `/home/dblagbro/docker/` (not the repo).
- `c1conversations-avaya-01-s23` is dropped and off limits; GCP is out of scope. Neither is
  part of this cluster.
- The `llm-proxy2-smoke` instance is gone: torn down on tmrwww01 on 2026-11-13, dormant
  compose service removed from tmrwww02 on 2026-11-17. Its volumes are kept.

## Working directories
| Path | Role |
|---|---|
| `/mnt/s/code/llm-proxy-v2` | **Source of truth.** Git worktree of `/mnt/s/code/llm-proxy`, branch `main`, on the `/mnt/s` NFS share that both nodes mount. |
| `/home/dblagbro/docker/build/llm-proxy-v2` | tmrwww01 build staging. Disposable rsync, overwritten by `stage-build.sh`. Never edit. |
| `/home/dblagbro/llm-proxy-v2` | Pre-move copy. Do not use. |

Build on tmrwww01: `/home/dblagbro/docker/scripts/stage-build.sh llm-proxy-v2 --build`
(rsync to local disk, then compose build). Current practice for tmrwww02: the image is built
on tmrwww01 and copied over with `docker save | docker load`, then the container is recreated
there.

## What works
- Both nodes serve v5.22.64 (see the banners at the top for what was verified per release;
  the v5.22.63 recreate was checked at `/health` 200, `healthy`, 7/7 providers).
- CI is green and blocking: full unit suite, hermeticity guard, secret scan, ephemeral
  integration suite, Playwright UI suite. See `docs/testing.md`.
- `tests/known_failures.txt` is empty. Inside the production image the unit suite showed 4
  environment-only failures at v5.22.63; they are described in that file.

## Known gaps
- **Grok-web is down until the operator signs in to grok.com on the bridge.** The bridge
  was rebuilt from `main` on tmrwww01 on 2026-11-17.
- **OpenAI API project is spend-capped.** The billing breaker holds it open (6 h hold, then
  re-probe) and requests fall back to other providers.
- **OpenRouter key is invalid.** The provider is enabled at priority 20 (last resort).
- **`make migrate` / `make migrate-new` refuse.** Alembic has no revisions; the schema comes
  from `create_all` plus in-place `ALTER TABLE` in `init_db()`.
- **Usage-reset advisor cannot redeem.** It alerts only; the redemption request has not been
  captured (see the section below).
- Triage lists: `tests/known_integration_failures.txt` (3 entries) and
  `tests/known_ui_failures.txt` (7 entries), each with recorded reasons.
- Older gap notes that were not re-verified for this rewrite (nginx log growth on the NFS
  share, tmrwww02 whole-stack compose error) are in the history file under "Other known gaps".

## Next actions
1. **Operator: sign in to grok.com on the bridge** so grok-web serves again.
2. **Operator: set the OpenAI API project spend limit**, or route GPT traffic through the
   ChatGPT-plan provider instead of the metered API project.
3. **Operator: replace the OpenRouter key.**
4. **Capture the claude.ai "Reset for free" request** from a real click, so the advisor's
   redemption step can be implemented.
5. **Parked:** v5.23 local-accelerator issues #1 and #5 to #10, pending a local model host.
   Spec: `docs/5.23-local-accelerator-orchestration-backpressure-design.md`.

## Resume commands
- Orient: read `AGENTS.md` + this file; `git -C /mnt/s/code/llm-proxy-v2 status -sb`.
- Health (per node): `curl -s https://www.voipguru.org/llm-proxy2/health` and
  `https://www2.voipguru.org/llm-proxy2/health`; check `status`, `version`, `healthyProviders`.
- Stage + build (tmrwww01): `/home/dblagbro/docker/scripts/stage-build.sh llm-proxy-v2 --build`
- Deploy (per node, needs approval): `cd /home/dblagbro/docker && sudo docker compose up -d --force-recreate --no-deps llm-proxy2 && sudo docker exec nginx nginx -s reload`
- Pool trace: `sudo docker kill --signal=SIGUSR2 llm-proxy2` then `docker logs --since 8s llm-proxy2`.
- Before any destructive action on a "remote" host, compare `hostname` and `/etc/machine-id`
  with the local values (2026-08-10 incident; see the history file).


## Voice-sidecar security candidate — 2026-11-09

Whisper4.3.1 is independently versioned in `whisper_bridge/VERSION`. Its pinned
asset/runtime recipe and offline speech checks are separate from the gateway
release. Gateway application and frontend code are unchanged. The tested candidate
is not yet deployed. The owner approved the Whisper-only scoped gates and
independent version exception on 2026-11-09 at 16:16:04 UTC. The 3,244
inherited gateway lint findings remain explicit debt; all final-head CI and
scoped checks must pass before release. Gateway 5.22.53 remains unchanged.
See [candidate evidence, limits and recovery](whisper-4.3.1-security.md).

## Usage-reset advisor (v5.22.59) - enabled on both nodes 2026-11-17

Advisory watcher for Anthropic Fable-only limit resets (`app/monitoring/usage_reset_advisor.py`,
flag `USAGE_RESET_ADVISOR_ENABLED`, default false; set true in both nodes' compose on
2026-11-17 with `USAGE_RESET_ADVISOR_FABLE_MIN_PP=20`). Emails the operator when weekly >= 95% and
Fable >= 30 pp of it, unless the weekly window resets within 2 h; once per window per account
(marker in `usage_reset_advisor_state`, not cluster-synced). Needs SMTP enabled to alert. Second
watcher: `scripts/usage_reset_advisor_cron.py` (no crontab installed). Redemption is NOT
implemented (`redeem_fable_reset()` raises; the endpoint must be captured from a real click). The
live `/usage` response exposes no "resets available" count, so that input is always None.
The existing 4-hourly scraper writes `external_usage_snapshot` and IS running (fresh rows);
the stale 2026-05-06 table is `provider_usage_windows`, a different (proxy-internal) table.
