> November 14 preparation: the Grok dedicated-network planner is tested source, **not deployed or wired into the release operator**. See [the network preparation limits](grok-bridge-security.md#dedicated-network-preparation-2026-11-14-not-deployed).

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

The 2026-11-11 banner below is kept as dated history.

---

# Live 2026-11-11 (v5.22.54, both nodes) — superseded 2026-11-13

**v5.22.54 deployed to tmrwww01 + tmrwww02** (rolling, peer-first; each node
verified `/health` version + healthy, RestartCount=0; public
https://www.voipguru.org/llm-proxy2/health → 200 v5.22.54). PR #12 merged to
origin/main (merge `1d27cb7`). QA sweep PASS (4101 unit pass / 2 skip / 0 fail;
auth, sub-path, path-traversal invariants green; cluster sync 200/fresh=true).
Per-node rollback tag `llm-proxy2:rollback-5.22.53-pre54` retained; pre-deploy
DB + config backups in `/home/dblagbro/backups/`.

**Provider fleet (2026-11-11, verified from activity_log):**
- **Both Anthropic Max OAuth grants recovered/healthy** — carrying traffic with
  0 errors (Max-VG + Max-Gmail). Resolves OPS-2026-11-06a.
- **Cohere + Codex + both Max** carry 100% of current load, 0 errors.
- **OpenRouter-Devin-Personal deweighted** (priority 6 → 20, still enabled,
  last-resort only): its API key is invalid (`sk-or-v1…` AuthenticationError)
  AND it was routing into OpenAI's spend cap. Refresh the key to restore weight.
- **OpenAI (Devin Personal ChatGPT) spend-capped** since ~Nov 9 — project
  `enforced spend limit` → RateLimitError on every probe; auto-skipped until
  2026-11-12 18:52. Raise the limit at platform.openai.com to restore. This is
  why OpenAI traffic went 3135 ok (Nov 7) → 0 (Nov 9–10). The 2026-11-11
  upstream OpenAI status-page outage is a non-event here (provider already dormant).
- **Grok-Web** still down (bridge token fixed; grok.com cookies not yet
  imported into `/home/dblagbro/.secret-handoff/grok.json` — backlogged per operator).

The 2026-11-07 pause block below is **superseded** — kept as dated history.

---

# ⏸ Paused 2026-11-07 ~01:47 EST (v5.22.53 live both nodes) — resume checklist ⟶ SUPERSEDED 2026-11-11 (see banner above)

Session paused with the proxy UP (non-premium providers serving) but **all three
premium providers down**, each needing an operator action I cannot perform
(OAuth login / credential / providers-table write are classifier-blocked):

1. **OPS-2026-11-06a — both Anthropic Max OAuth grants REVOKED** (`401 "OAuth access
   token has been revoked"`, `error_class=auth`). Max-VG failing since ~11:09 UTC
   Nov 6, Max-Gmail since ~12:28 UTC Nov 6. Will NOT self-heal (revoked, not expired).
   → **Action:** admin UI → Providers → reconnect/re-authorize `Devin-Anthropic-Max-VG`
   AND `Devin-Anthropic-Max-Gmail`. (Two accounts revoked ~1h apart — check the
   Anthropic account side for why.) Details in `bug-log.md` OPS-2026-11-06a.

2. **💸 OpenAI cost bleed (consequence of #1).** With Max down, `tax-ai-analyzer`'s
   steady ~140 req/hr gpt-4o job FAILED OVER to the metered `Devin Personal OpenAI
   ChatGPT` account. OpenAI MTD ~$17.6 (Nov 6 = $8.00 vs ~$1.5/day baseline);
   projects ~$240/mo while Max stays down — this tripped the $20/mo alert.
   → **Fix = do #1.** Re-auth routes tax-ai back to the unmetered Anthropic
   subscription; OpenAI drops to ~$1/day. Longer-term: consider capping/pinning the
   tax-ai key so a provider outage degrades instead of silently spending on OpenAI,
   and confirm tax-ai's 140 req/hr 24x7 volume is intended.

3. **Grok-Web — two independent breaks (post-reboot):**
   a. `extra_config.bridge_token` is EMPTY → bridge rejects "invalid bridge token".
      → **Action:** `bash /home/dblagbro/fix-grok-bridge-token.sh` (copies BRIDGE_TOKEN
      from `/home/dblagbro/docker/.env` into the provider; never prints it; classifier-
      blocked for me, operator must run).
   b. Bridge not logged into grok.com (`logged_in:false`, no `sso`/`x-userid`).
      → **Action:** export grok.com cookies (Cookie-Editor → JSON) to
      `/home/dblagbro/.secret-handoff/grok.json`, then
      `bash /home/dblagbro/import-grok-cookies.sh /home/dblagbro/.secret-handoff/grok.json`.
      BOTH a+b needed before grok-web serves.

Env otherwise clean through v5.22.53; no code change pending. Breakers are
in-memory and reset on container recreate — they re-trip on live failure, so
`/health` state is authoritative. Pre-existing operator-gated backlog unchanged:
BUG-113 (www1→www2 sync path, infra), Fix B dedupe+UNIQUE migration, O-3 rotate
CLUSTER_SYNC_SECRET.

**Lower-priority signals observed during the pause (2026-11-07, watch — not blocking):**
- **OpenRouter-Devin-Personal** (`a40257f80cf0cae9`, openrouter): intermittent
  `AuthenticationError` — the OpenRouter API key looks invalid/expired. Flaps
  half-open; verify/refresh the key when convenient.
- **Devin-Codex-Gmail** (`c549ed05a1cd86d3`, ChatGPT-oauth-plan): a one-off upstream
  `400 {"detail":"Unsupported parameter: temperature"}` (a caller sent `temperature`
  to a codex model that rejects it) — 0 recurrences, not a provider fault. Also the
  usual benign `ConnectTimeout` keepalive-probe flaps.
- One transient multi-provider dip (OpenAI+Cohere `network`/"Cannot connect" ~18:46
  UTC) that self-cleared; container egress to all upstreams verified healthy, so it
  was an upstream/transit blip, not a local outage.
- Candidate improvement still noted: keepalive-probe `ConnectTimeout`s counting toward
  the same breaker that gates real traffic is what makes healthy providers flap in
  `/health`; a one-retry-before-count (or not counting probe transport-timeouts) would
  stop the cosmetic flapping without masking real outages. Work-item, not urgent.

---

# Live now — 2026-10-29 (v5.22.46)

**Both nodes serve v5.22.46** (rolling, tmrwww01 then tmrwww02; verified via
`/health` on each node + public LB). This session fixed a cross-node
availability bug plus (earlier) three code/config regressions and a frontend
render-loop, all triggered while the operator was editing/re-authing the
Anthropic Max OAuth accounts. **Account-side blockers that only the operator
can clear remain open — see the bottom of this block.**

- **⏳ v5.22.47 (Daybreak Blue) — BUILT + unit-tested, AWAITING DEPLOY
  APPROVAL (not live).** Adds OpenAI Daybreak Blue (Trusted Access for Cyber)
  passthrough on the Codex/SIWC path — the channel that survives OpenAI's
  2026-11-20 individual-API-key cutoff. Daybreak is a per-request field
  (`access_programs.cyber`), defaulted OFF (`standard`) by OpenAI even on an
  approved account; verified 2026-10-30 that the live approved Gmail account
  accepts `daybreak_blue` and the Codex endpoint validates the enum
  server-side. Opt-in ONLY via a `daybreak`/`daybreak-blue` model alias
  (pins the approved provider, rewrites to `codex-auto-review`); gated to keys
  with `daybreak_tier="blue"`; `daybreak_red` hard-blocked; every call audited
  to `compliance_events`. Code + 18 unit tests green; full unit suite 4066
  passed (the 23 `test_record_outcome_meta` errors are a pre-existing
  cross-test ordering flake — the file passes standalone). **Deploy-time data
  steps still pending:** (1) create the `daybreak`/`daybreak-blue` ModelAlias
  rows → Devin-Codex-Gmail / `codex-auto-review`; (2) mark that provider's
  `extra_config.daybreak_tier="blue"` (detection signal; VG stays unmarked);
  (3) authorize ONE operator-chosen key with `daybreak_tier="blue"`. See
  `app/providers/daybreak.py` and CHANGELOG v5.22.47.

- **BUG-112 (v5.22.46) — a re-authed OAuth account stayed broken on the PEER
  node.** `oauth-rotate`/background refresh `force_close` the circuit breaker
  only on the node that served the re-auth; that reset is in-memory/node-local
  and never synced. The fresh token DID propagate to the peer (DB columns
  `api_key`/`oauth_refresh_token`/`oauth_expires_at`), but the peer's breaker
  stayed OPEN on its stale exponential hold-down — so the account still failed
  on ~half of load-balanced traffic. Observed right after the operator re-authed
  both Max accounts: `d5123a00…` (VG) healthy on both nodes, but `91bafda9…`
  (Gmail) sat half-open with 15 failures / consecutive_opens=5 on www2 while
  closed on www1. Fix: `cluster/sync_handlers._apply_providers` now snapshots
  the three credential columns before the merge and, when any changed,
  `force_close` + `clear_auth_failure` on the receiving node — a token rotation
  anywhere converges the breaker everywhere within one sync cycle (≤60s;
  immediate via `/cluster/sync-now`). Non-credential edits leave an open
  breaker alone. The container recreate on deploy also clears the stale
  in-memory breaker immediately. Tests: `test_v52246_sync_resets_breaker_on_reauth.py`.

- **BUG-111 (v5.22.45) — OAuth Accounts panel polled in a render loop.**
  `useToast()` returned a fresh object every render, making
  `OAuthAccountsPanel.load` unstable and re-firing its effect every render →
  `GET …/oauth-accounts` thousands/sec (all 200 at the backend), which while
  editing a provider surfaced as a flood of red "Failed to load OAuth
  accounts" toasts. Fixed: `useToast` returns a stable module-level singleton.
  Frontend — operator must hard-refresh to load the new bundle.
- **O-2 allowlist 403'd inter-node cluster sync (same session).** Once the 413
  (below) was fixed, the O-2 `/cluster/sync` allowlist began returning 403:
  inter-node sync reaches the peer via the public hostname, hairpins through
  the site WAN, and arrives from `24.168.14.36` — not a private IP. Fixed:
  `allow 24.168.14.36;` added to all four cluster/sync blocks (both nodes).
  Sync verified `200` again; 0 rejections. HMAC remains the second layer;
  O-3 (strong secret) still the right hardening, and moving sync to a
  private-LAN path would remove the WAN hairpin entirely (not yet done).

The two regressions reported first (operator could not re-auth
`Devin-Anthropic-Max-VG`):

- **BUG-109** — the v5.22.24 provider-type retirement gate (a pydantic
  `field_validator` inherited by `ProviderUpdate`) 422'd *edits/re-auth* of an
  existing deprecated provider, not just creation. `PUT /api/providers/{id}`
  re-submits the row's own `claude-oauth` type → 422, blocking the UI
  save-after-re-auth. Fixed: create gate stays absolute; updates go through
  `validate_provider_type_change(old, new)` in `update_provider()` — keeping a
  closed type is allowed, only a *change into* one is refused. The
  `oauth-rotate` path was never broken (rotate returned 200, provider test
  200); the token is fresh and the account is serving.
- **BUG-110** — the O-2 nginx `/cluster/sync` allowlist blocks omitted
  `client_max_body_size`, inheriting the 1M default while the general block
  sets 50M; `apply_sync` payloads exceeded 1M → peer nginx 413 → cluster sync
  silently stopped. Fixed: `client_max_body_size 50M;` on all four cluster/sync
  blocks, both nodes (nginx reload, not an image change). Verified: www2 now
  returns 403 (HMAC), not 413, on a 2MB body.

**Account-side blockers — operator action, not fixable in code:**
- **`Devin-Anthropic-Max-VG` (claude-oauth) is failing upstream.** Anthropic
  returns HTTP 400 `invalid_request_error`: *"We've updated our Consumer Terms
  and Privacy Policy. You'll need to accept them in claude.ai…"*. The proxy
  re-auth/rotate works (stores a fresh token, test 200); Anthropic holds the
  account behind a terms interstitial. Operator must sign into claude.ai with
  that account and accept. This is the subscription-OAuth enforcement the
  2026-09-26 market review predicted. `Devin-Anthropic-Max-Gmail` token
  expired ~40 days ago (separate).
- **grok-bridge (tmrwww01 only) — browser had crashed; restarted.** Its
  Playwright browser died (`TargetClosedError`) while the container stayed
  "healthy" (healthcheck pings the HTTP server, not the browser), so creates
  500'd. `docker restart llm-proxy2-grok-bridge` relaunched it; grok.com loads
  again. But `/api/status` shows `logged_in:false` (sso/x-userid cookies lost
  in the crash) — operator must re-login via `/grok-bridge/login` or the VNC
  view. `grok-web` is a DEPRECATED type (no new providers). Follow-up idea:
  make the sidecar healthcheck probe the browser so Docker auto-restarts it.

Earlier operator items from the v5.22.43 pass: **O-1** (per-node `SECRET_KEY`
rotation, GPG-sealed) and **O-2** (cluster/sync source-IP allowlist) are DONE
on both nodes. **Still open:** O-3 (rotate the 19-char `CLUSTER_SYNC_SECRET`
to 32+, lockstep both nodes — the boot log still warns), O-7 (encrypt
provider/OAuth creds at rest). Local commits for v5.22.43/44/45 are unpushed
(origin CI is an operator decision).

---

# Resumed 2026-08-28 — audit re-verified

Re-scan done on resume (the pause block said not to trust the 2026-08-12
findings because a rename was reported). Result: **no rename is visible.**
`git remote` still reads `dblagbro/llm-proxy-manager`; the GitHub API returns
`full_name: dblagbro/llm-proxy-manager`, `private: false`; and that is the only
llm/proxy repo under the account. GitHub follows renames to the canonical name,
so if a rename happened it was to a different repo. **The repo is still public.**

## Credential status, re-verified 2026-08-28

| Credential | Still published? | Still works? |
|---|---|---|
| admin password (2 integration test files) | YES on `origin/main` | **NO — rotated** (401 for `admin` and `dblagbro` on both nodes) |
| `DEFAULT_BRIDGE_TOKEN` (grok provider form) | removed from source | **NO — ROTATED 2026-08-28** |

**ROTATED 2026-08-28.** Root cause: `docker-compose.yml` read
`BRIDGE_TOKEN=${BRIDGE_TOKEN:-<literal>}` and `.env` never set it, so the
value published in the repo *was* the live token. Now: a 52-char random token
lives in `/home/dblagbro/docker/.env`, and the compose default is
`${BRIDGE_TOKEN:?...}` so it can never silently fall back to a literal again.
Verified: the old published token returns **401**, the new one is accepted, and
the grok provider's `bridge_token` was updated on **both** nodes.

The fix commit was never pushed, which is why both values are still on
`origin/main`. Its changelog entry named the token value verbatim, so it was
redacted before shipping — pushing must not re-publish a credential that is
still valid.

## State — re-verified live 2026-09-06

- `origin/main` == local `main` == **v5.22.15**. The v5.22.14 audit fix **was pushed**
  (an earlier note here claimed it had not been — that was stale).
- Live: tmrwww01 + tmrwww02 both **v5.22.14 healthy**, 7/7 providers, `checked_out=0` —
  the `_next_route` wedge fix is holding ~4 weeks on. v5.22.15 is not deployed yet.
- grok-bridge healthy, scraping replies; its `bridge_token` matches `/home/dblagbro/docker/.env`
  exactly. The "grok-web is down" reading in the UI is a **stale circuit breaker**, not a
  dead bridge — its last recorded failure bucket is 2026-09-01.

## State — 2026-09-16

- Local `main` = **v5.22.18**, **4 commits ahead of origin** (v5.22.15–18). `origin/main`
  is still `9766d8b` (v5.22.14). Nothing pushed, no history rewrite.
- **Security self-test 2026-10-11 → `docs/security-review-2026-10-11.md`.** Fixed in
  source (v5.22.43, undeployed): BUG-098 unauth file-read path traversal, BUG-099 MCP
  policy fail-open, BUG-104 PII-regex ReDoS, BUG-108 secret-redaction gaps, plus
  BUG-100/101/102/103/105/106/107. **Operator-only and still live:** O-1 default
  `SECRET_KEY` (every stored key decryptable), O-2 the `/cluster/sync` nginx allowlist
  is absent from the running config, O-3 short/shared/hardcoded secrets, O-4 spoofable
  client IP. Needs a decision: O-6 (BUG-109) the compliance audit chain hashes 5/14
  columns and is never verified; O-7 provider/OAuth creds stored plaintext.
- **Live: both nodes v5.22.14, healthy, 7/7 providers.** So four versions of fixes —
  including the cluster body-read race and the claude-oauth self-heal — are committed
  but NOT deployed.
- Backups taken 2026-09-16 before any deploy: `llmproxy.www{1,2}.pre-v52218.*.bak`
  (`integrity_check: ok`) plus a repo mirror `llm-proxy-repo.*.git`, all in
  `/home/dblagbro/backups/`.
- `app/api/messages.py` and `bug-log.md` have carried another session's uncommitted
  work since 2026-09-11 (the Codex `xai/` diagnostic + its bug-log entry). Left
  untouched. **If that session is gone, this work is orphaned in the working tree and
  a checkout would discard it** — worth resolving.

## Cost audit — 2026-09-16 (OpenAI key `sk-…jogA`)

Triggered by a $20/month notification. Conclusion: **the bill is legitimate and
correctly priced; only the proxy's own reporting is low.**

| | |
|---|---|
| Dashboard, Sept MTD | $20.05 |
| Proxy-attributed, Sept MTD | $12.58 → **37% low** |
| Dashboard input tokens, Sep 09–16 | 4,354,580 |
| Proxy input tokens, same window | 2,629,493 → **40% low** |

The two gaps agreeing is what makes it a counting problem rather than a pricing one.
Rate check: ~all traffic on that key is `openai/gpt-4o`; proxy's implied rate is
$2.54/1M input, actual $2.45/1M, against a $2.50 list price. Independently,
8.19M input tokens at list = $21.09 vs $20.05 billed (5% variance).

Not leaked: 0 of 703 commits contain the key, and it is absent from the working tree.

### BUG (FIXED — v5.22.20 + v5.22.51) — CoT upstream calls now recorded in `provider_metrics`

**Resolved.** v5.22.20 added `app/cot/usage.py` (request-scoped ContextVar
accumulator; `_call` folds each critique pass via `accumulate_usage`) and wired
`reset_cot_usage()`/`fold_cot_usage()` into the Anthropic `/v1/messages`
streaming path. v5.22.51 wired the same into the **OpenAI
`/v1/chat/completions` streaming path** (`_completions_streaming.py`), which the
original fix missed — so both streaming CoT paths now fold internal-call tokens
before `record_outcome`. `run_cot_pipeline` runs only in those two streaming
entrypoints (the non-streaming dispatch never invokes CoT), so coverage is now
complete. `test_v52220_cot_usage_accounting.py::TestWiring` guards both files.

Original (pre-fix) note: `cot_enabled=True`, `cot_max_iterations=4`.
`app/cot/pipeline.py` loops and issues an upstream `_call(...)` per pass but
recorded **zero** metrics — a 1.66× multiplier on recorded traffic, making
per-key spend caps unreliable.

Consequence worth acting on: **per-key spending caps are unreliable.** A cap enforced
on a token count that is ~40% low would let real spend run well past it. Do not rely
on `spending_cap_usd` / `daily_hard_cap_usd` until this is fixed and the numbers
reconcile against the vendor dashboard. No key or provider currently sets a cap.

Needs a `bug-log.md` entry once the other session's changes to that file land.

Side observation, not yet chased: 6.4 output tokens per recorded request (134:1
input:output). Plausibly just an artefact of the same bug — the substantial calls
being the unrecorded ones — but this repo has an `empty_success_burst_trigger` worker
and v5.22.13 dealt with empty-completion guards, so it deserves a look.

## Open, verified 2026-09-06

1. **`Devin-Codex-Gmail` is failing 100% of what it receives** — the never-re-arming
   `auto_skip` is **FIXED in v5.22.16** (rate-based escalation had a blind spot for
   low-traffic dead providers); the provider itself still needs a reauth.
   Original note: (55/55 over 6h; still failing
   at 23:35 UTC). `auto_skip_until` expired **2026-08-20**, so the skip never re-arms and the
   breaker flaps open→hold-down→closed forever. Because `/health` samples an instant, a
   flapping provider reads as healthy and the node reports 7/7 — the UI structurally cannot
   see this. Errors come back as `XaiException` on a `ChatGPT-oauth-plan` provider with an
   empty `base_url`, so the upstream is misrouted, not just mis-keyed. Reauth had not landed
   as of 2026-09-06 (`updated_at` still 2026-08-28 21:29:25 UTC).
2. ~~**Cluster sync www1 → www2 is rejected**~~ — **ROOT-CAUSED AND FIXED in v5.22.16.**
   It was a body-read race, not a signature problem: the disconnect watchdog's
   `is_disconnected()` consumes and discards buffered `http.request` chunks, so a large
   body arrives short. Proved by posting identical bytes + signature six times for
   `200,200,200,403,200,200`. Affected every body-bearing endpoint, not just sync.
   Original note: **Cluster sync www1 → www2 is rejected** — `403 Invalid cluster signature`, 229 times in
   16h. Inbound www2 → www1 is fine. Ruled out: the secret matches on both nodes (identical
   md5), the peer URL resolves to the real www2, and the nginx location blocks are identical.
   A **small** signed body is accepted (verified: `POST /cluster/sync` with a 44-byte payload
   returns 200), so the failure is size- or content-dependent on the full payload. Not yet
   root-caused.
3. **Secrets remain in git history** on a public repo. Both are rotated and dead, and HEAD is
   clean, but history still holds them. Purging rewrites every hash and needs explicit
   operator approval — not done.
4. **Tree divergence unresolved:** compose builds from `/home/dblagbro/llm-proxy-v2`, not this
   canonical tree.
5. **`tests/conftest.py` session fixtures authenticate against LIVE production.** This is the
   stated blocker on CI gating the full suite, and it means running the suite locally mutates
   production. Highest-leverage test-infra fix available.

## v5.22.15 — security pass (2026-09-06)

Fixed: cluster HMAC failed **open** on an unset secret (empty key = publicly known key →
forgeable `apply_sync`); admin login had no attempt limiting; a live-shaped API key was still
published in `docs/qa-notes.md`. Added `tools/secret_scan.py`, `.githooks/pre-commit`, a CI
`secrets` job, and hardened `.gitignore`/`.dockerignore`. CI gating grew 9 → 13 files; both
credential guards now actually run there. See `CHANGELOG.md`.

Note for deploy: both live nodes carry a 19-character `CLUSTER_SYNC_SECRET`. v5.22.15 does not
reject it, but will log a length warning at boot recommending 32+.

---

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
- Version **v5.22.11**, tagged and released; the version commit is `0205f6d`.
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
| `/home/dblagbro/docker/build/llm-proxy-v2` (www2) | tmrwww02 build staging — **aligned with tmrwww01 on 2026-08-18**. Same disposable-rsync rule. |
| `/home/dblagbro/llm-proxy-v2` | **Pre-move copy — do not use, no longer referenced by anything.** tmrwww01: `.git` points at a worktree registered elsewhere. tmrwww02: pointer is dangling → orphaned non-git copy. |

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
5. ✅ **tmrwww02 migrated to the staging pattern (2026-08-18).** `stage-build.sh` installed there,
   the SAN source staged to `/home/dblagbro/docker/build/llm-proxy-v2` (18,336 files, hash
   `f4ea143f1ca464e7` — identical to the SAN tree), and compose flipped off the orphaned
   `build: /home/dblagbro/llm-proxy-v2`. Both nodes now build the same way. Note the sync took
   hours over NFS: ~305 MB of many small files is metadata-bound, not throughput-bound.
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
- **Test state as of v5.22.42.** `known_failures.txt` EMPTY; unit suite 4044 passing and
  gated; integration suite 63 passing against a throwaway instance it boots itself
  (`LLMPROXY_TEST_EPHEMERAL=1`); Playwright UI 63 of 73 running, having run **nowhere**
  before v5.22.42. Two triage lists remain, both short and both with recorded reasons:
  `tests/known_integration_failures.txt` (3 — a CoT/tool-emulation precedence question
  that needs an operator decision) and `tests/known_ui_failures.txt` (6 — need a richer
  seed in the ephemeral fixture, not test changes).
- **Clearing those lists has been the highest-yield work in the project's history:**
  eight real defects nothing had ever exercised — BUG-086…BUG-097 plus F-INFRA-004 —
  including a live compliance bug (BUG-093, sovereignty constraints narrowed *and*
  unenforced) and two accounting bugs that made cost read zero (BUG-095, BUG-096).
- **`make migrate` does not work and never did.** No `alembic/versions/`, zero
  revisions. Schema comes from `create_all` plus in-place `ALTER TABLE` in `init_db()`.
  The targets now refuse and explain. Adopting alembic needs a baseline revision
  stamped on every node — real work, operator's call.
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


## Voice-sidecar security candidate — 2026-11-09

Whisper4.3.1 is independently versioned in `whisper_bridge/VERSION`. Its pinned
asset/runtime recipe and offline speech checks are separate from the gateway
release. Gateway application and frontend code are unchanged. The tested candidate
is not yet deployed. The owner approved the Whisper-only scoped gates and
independent version exception on 2026-11-09 at 16:16:04 UTC. The 3,244
inherited gateway lint findings remain explicit debt; all final-head CI and
scoped checks must pass before release. Gateway 5.22.53 remains unchanged.
See [candidate evidence, limits and recovery](whisper-4.3.1-security.md).

## Usage-reset advisor (v5.22.59) - built, OFF

Advisory watcher for Anthropic Fable-only limit resets (`app/monitoring/usage_reset_advisor.py`,
flag `USAGE_RESET_ADVISOR_ENABLED`, default false). Emails the operator when weekly >= 95% and
Fable >= 30 pp of it, unless the weekly window resets within 2 h; once per window per account
(marker in `usage_reset_advisor_state`, not cluster-synced). Needs SMTP enabled to alert. Second
watcher: `scripts/usage_reset_advisor_cron.py` (no crontab installed). Redemption is NOT
implemented (`redeem_fable_reset()` raises; the endpoint must be captured from a real click). The
live `/usage` response exposes no "resets available" count, so that input is always None.
The existing 4-hourly scraper writes `external_usage_snapshot` and IS running (fresh rows);
the stale 2026-05-06 table is `provider_usage_windows`, a different (proxy-internal) table.

Network preparation update (2026-11-14): a sanitized local Docker adapter now
accompanies the dedicated-network planner and consumer journal. Read-only is the
default; explicit transaction/container bindings gate mutations. This is unshipped
preparation with no release-operator integration or production change. See
[security preparation](grok-bridge-security.md).
