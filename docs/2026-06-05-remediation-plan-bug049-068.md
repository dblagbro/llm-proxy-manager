# Remediation Plan — 2026-06-05 QA sweep findings (BUG-049 … BUG-068)

Owner: llm-proxy team
Source: `bug-log.md` (BUG-049..068), `test-plan.md`, `qa-notes.md`,
`architecture.md`, `design.md`, `refactor-log.md`.
Scope: **17 open findings** + 3 fixes-already-shipped-during-sweep
(BUG-049/050/051 are listed for completeness but excluded from the
plan).

> **No implementation in this document.** Stops at planning.

---

## 1. Executive summary

| | Count | Severity ladder |
|---|---|---|
| Critical, open | 2 | BUG-052, BUG-053 |
| High, open | 4 | BUG-054, BUG-055, BUG-056, BUG-057 |
| Medium, open | 4 | BUG-058, BUG-059, BUG-060, BUG-061 |
| Low, open | 7 | BUG-062, BUG-063, BUG-064, BUG-065, BUG-066, BUG-067, BUG-068 |
| Total open | **17** | |
| Critical, fixed during sweep | 3 | BUG-049, BUG-050, BUG-051 |

Open findings group into **four root-cause clusters**:

| Cluster | Root cause | BUGs | Touched files |
|---|---|---|---|
| **A. Bridge concurrency model** | Single Chromium tab driven by Playwright with no per-page lock around the new SPA-UI code paths. | 052, 054, 055, 058, 060, 065, 067, 068 | `grok_bridge/app.py` only |
| **B. Provider error mapping** | Per-bridge error translation lacks a shared contract; downstream proxy trusts `200 OK` from sidecars. | 053 | `cursor-bridge` repo (separate) + `app/providers/cursor_*` + a new generic response-validator middleware |
| **C. v5.0.18 cluster_peers hardening** | New feature shipped with insufficient defensive coding. | 057, 059, 061, 062, 063, 064, 066 | `app/cluster/sync_handlers.py`, `app/cluster/manager.py`, `app/api/cluster.py`, `frontend/src/pages/ClusterPage.tsx` |
| **D. Deploy & ops hygiene** | Leftover dev compose file, missing log breadcrumbs. | 056 | `/home/dblagbro/llm-proxy-v2/docker-compose.yml` (delete or rename) + `CLAUDE.md` |

The plan ships these as **5 fix batches** ordered by blast radius and
risk; each batch is independently reversible.

---

## 2. Risk-controlled fix batches

### Batch 1 — Ops hygiene (low risk, immediate)

**Bugs:** BUG-056 (stray repo-root compose), BUG-066 (silent
CLUSTER_PEERS no-op), BUG-067 (misleading retry comment).
**Why first:** zero code paths touched; eliminates a recurring deploy
trap that costs ~15 min per occurrence and was hit 3× during the
sweep itself.

**Scope:**
- Delete or rename `/home/dblagbro/llm-proxy-v2/docker-compose.yml` to
  `docker-compose.dev.yml.example` (preserve git history; add a note
  in the file header explaining what it WAS and why it was renamed).
- Update `CLAUDE.md` "Docker rules" section: explicit `cd /home/dblagbro/docker`
  prelude for every `docker compose` example.
- `app/cluster/manager.py::_seed_peers_from_env_if_empty`: add an
  `INFO` log line when env CLUSTER_PEERS is set but DB already has
  rows, listing both for operator awareness.
- `grok_bridge/app.py:1532`: remove the misleading "we capture a fresh
  statsig-id" comment (it's accurate for the now-removed httpx path,
  not for SPA-UI).

**Pre-flight backups:** None required (no code-behavior changes that
can corrupt state). Snapshot the current `docker-compose.yml.example`
location so the rename is recoverable.

**Rollback:**
- Frontend / state: not touched.
- Bridge / proxy: not touched.
- Restore the stray compose file by renaming back: `mv
  docker-compose.dev.yml.example docker-compose.yml` and `git commit
  --revert`.

**Retest after batch:**
- Run `python3 -m pytest tests/unit/ -q` — expect 2670 passing.
- `cd /home/dblagbro/llm-proxy-v2 && sudo docker compose up -d --no-deps
  llm-proxy` MUST fail explicitly (the rename guarantees this).
- `cd /home/dblagbro/docker && sudo docker compose up -d --no-deps
  llm-proxy` MUST succeed.
- Re-deploy v5.0.21+1 (next tag) and confirm all 5 endpoints come back
  on the new version within 60 seconds.

**Effort estimate:** 30 min.

---

### Batch 2 — Grok bridge concurrency hardening (high value, medium risk)

**Bugs:** BUG-052 (concurrent /api/chat race, CRITICAL), BUG-054
(listener leak on early returns, HIGH), BUG-055 (cookie_refresh races
with /api/chat goto, HIGH), BUG-058 (capture_next_send not under lock,
MEDIUM), BUG-065 (context listener leak on shutdown, LOW), BUG-068
(SPA-UI vs UI-send duplication, LOW).

**Why batched:** All in `grok_bridge/app.py`; all root-cause-share the
missing lock around `_page` mutations. Atomic fix is easier to reason
about than 6 separate changes.

**Scope (architectural):**
- Add a wrapper `async def _with_page_lock(coro)` and audit every
  function that calls `_page.goto`, `_page.locator`, `_page.keyboard`,
  `_page.on/remove_listener`. Wrap each public endpoint
  (`chat`, `create_new_conversation`, `capture_next_send`,
  `_cookie_refresh_loop`) so they hold the lock for the duration of
  their `_page` interactions.
- Refactor `_send_via_spa_ui` to install + remove the response
  listener in `try/finally` (closes BUG-054 — early-return leaks).
- Extract a shared `_drive_spa_send(conv_id, message)` helper used by
  both `chat` and `create_new_conversation` (closes BUG-068).
- Lifespan teardown: `_context.remove_listener("request",
  _on_context_request)` (BUG-065).

**Scope (local):**
- Comment fix BUG-067 (already covered by Batch 1).

**Pre-flight backups:**
- Snapshot the bridge container image tag CURRENTLY deployed
  (`sudo docker images llm-proxy2-grok-bridge --format
  "{{.ID}} {{.CreatedAt}}"`). Pin as `llm-proxy2-grok-bridge:rollback-2026-06-05`.
- Copy `/data/playwright-state` (logged-in session state) before the
  container restart: `sudo tar czf /tmp/playwright-state-$(date +%s).tgz
  -C /var/lib/docker/volumes/docker_llm-proxy2-grok-bridge-state/_data .`
  (path under volume mount point — confirm with `docker volume
  inspect` first).

**Rollback:**
- Restore the bridge to `:rollback-2026-06-05` image:
  `sudo docker compose up -d --force-recreate --no-deps
  llm-proxy2-grok-bridge` after retagging.
- If Playwright session corrupted: restore the tarball into the volume,
  recreate the container, re-verify cookies via `/api/status`.
- Expected downtime during rollback: ~30 seconds per bridge.

**Retest after batch:**
- **Smoke:** `/api/status` returns `logged_in: true`, all 4 cookies
  present, statsig_cache warms within 60s of recreate.
- **Concurrency reproducer (the BUG-052 test case):** fire 2
  simultaneous `/api/chat` POSTs with distinct user messages against
  the same `conversation_id`. Both responses MUST be non-empty AND
  MUST contain content responsive to their respective messages.
  Acceptance: 10 consecutive 2-call concurrent runs, all 20 responses
  correct.
- **Listener leak repro:** trigger a deliberate textarea-not-found
  return (e.g., navigate to `grok.com/` instead of `/c/<id>`). Then
  immediately fire a normal `/api/chat`. Confirm the legitimate chat
  succeeds (no dangling listener corrupting its response).
- **Refresh race repro:** override `COOKIE_REFRESH_INTERVAL_SEC` to 30
  for the test; fire continuous `/api/chat` calls for 5 minutes;
  confirm NO calls return `chat-submit button not found` due to the
  refresh tick.
- **New unit tests:** add a test that mocks Playwright `_page` and
  verifies `_lock` is acquired in each of: `chat`, `create_new_conversation`,
  `capture_next_send`, `_cookie_refresh_loop`. Source-pin lock
  acquisition via grep assertion in the same style as
  `test_v5021_disable_long_context.py`.
- Live end-to-end: provider Test on Grok-Web-Devin via the UI Test
  button returns success.

**Effort estimate:** 4-6 hours code + 1h tests.

---

### Batch 3 — Cursor bridge error mapping (critical, isolated)

**Bugs:** BUG-053 (cursor-bridge returns 200 for plan-downgrade errors).

**Why standalone:** Only 1 bug in the batch but it spans two repos
(cursor-bridge source repo + proxy `app/providers/`); needs separate
deployment artifact; risk profile is different (touches an external
provider integration).

**Scope (per-bridge):**
- `cursor-bridge` source: detect Cursor's structured error JSON
  (`code: "resource_exhausted"`, `ERROR_RATE_LIMITED_CHANGEABLE`,
  `ERROR_AUTH`, others) and translate to:
  - 429 with `Retry-After` for `ERROR_RATE_LIMITED_CHANGEABLE` /
    `resource_exhausted`
  - 401 for auth errors
  - 502 for upstream Cursor failures
- Surface the error message in the response body so the proxy can
  record it.

**Scope (proxy-side generic guard):**
- New middleware `app/api/_response_validators.py::reject_empty_success`
  that flags any provider response with `usage.total_tokens == 0 AND
  content == "" AND finish_reason == "stop"` as upstream failure (raises
  `HTTPException(502, "upstream returned empty success")`). Apply to
  the cursor-oauth dispatch path AND any future bridge with similar
  potential.
- `app/providers/cursor_bridge_*`: parse the now-non-200 cursor-bridge
  response correctly + emit `circuit_breaker.record_failure` on the
  account-downgrade error class.

**Scope (observability):**
- `app/providers/cursor_billing.py` worker (per memory:
  `project_backlog_cursor_oauth_usage_monitoring`): extend the scrape
  to capture `plan_tier` (Pro / Free / Trial-Expired); compare against
  prior tier; on downgrade, set `auto_skip_until = now + 24h`,
  `manual_override_reason = "plan_tier_downgraded: <new>"`, and emit a
  high-priority activity log entry.

**Pre-flight backups:**
- Snapshot the current Cursor-oAuth-C1acct provider row from BOTH
  clusters (llm-proxy + llm-proxy2): `extra_config`, `enabled`,
  `manual_override_*`. The auto-skip-until edit can be reverted by
  setting these back.
- Tag the current `llm-proxy2-cursor-bridge:latest` as
  `llm-proxy2-cursor-bridge:rollback-2026-06-05`.

**Rollback:**
- Cursor-bridge container: `sudo docker compose up -d --force-recreate
  --no-deps llm-proxy2-cursor-bridge` after retagging.
- Proxy: rollback to v5.0.21 image (still pinned).
- Provider row: restore extra_config from snapshot.

**Retest after batch:**
- **Cursor-bridge fixture test:** feed the captured downgrade JSON to
  the bridge's error handler; assert HTTP 429 + `Retry-After` header.
- **Proxy guard test (new):** mock a provider returning `{"content":
  [], "usage": {"total_tokens": 0}}`; assert proxy returns 502 + the
  circuit breaker records a failure.
- **Live e2e (BLOCKED until Cursor plan is upgraded OR a Free-tier
  test account is available):** request `claude-haiku-4-5` through the
  clone with the DevinGPT key. Pre-fix: empty content + success.
  Post-fix: 429 with `Retry-After` (and rotation skips to next
  provider, returning real content from Anthropic-Max).
- **Operator notification:** confirm activity log gains a "Cursor plan
  downgrade detected" entry within 5 min of the next scrape.

**Effort estimate:** 6-8 hours (split across cursor-bridge repo + proxy
+ scraper). Account-downgrade testing requires either a Free-tier
account fixture or live operator coordination.

---

### Batch 4 — Cluster_peers v5.0.18 hardening (medium risk, data-touching)

**Bugs:** BUG-057 (self-row phantom on NODE_ID change, HIGH), BUG-061
(reload_peers race, MEDIUM), BUG-062 (HTTP URLs allowed, LOW),
BUG-063 (Playwright-unfriendly confirm(), LOW), BUG-064
(_parse_iso_keep_naive type confusion, LOW).

**Why batched:** All in the v5.0.18 cluster_peers feature, all touch
`cluster_peers` table or its admin API.

**Scope (architectural):**
- BUG-057 prune-on-startup: `app/cluster/manager.py` lifespan startup
  runs `DELETE FROM cluster_peers WHERE id = ?` for the current
  `cluster_node_id` BEFORE any sync push. Log how many rows were
  pruned (should be 0 in normal operation; a non-zero count indicates
  a recent NODE_ID rename).
- BUG-061: add `asyncio.Lock` around the in-memory `_peers` swap in
  `_reload_peers_from_db`.

**Scope (local):**
- BUG-062: `app/api/cluster.py::add_cluster_peer` rejects URLs without
  `https://` prefix (allow `http://` only when `DEBUG=true` env is set,
  for local development).
- BUG-064: `_parse_iso_keep_naive` returns `None` on unrecognized
  types, with a `logger.warning` recording the bad value (helps
  diagnose buggy peer payloads).
- BUG-063: replace `confirm()` with a `<ConfirmModal>` component
  (codebase already has one used elsewhere in `frontend/src/components/ui/`).

**Pre-flight backups:**
- Snapshot the `cluster_peers` table on ALL nodes (llm-proxy2 +
  llm-proxy on both tmrwww01 and tmrwww02 + c1conv): `sudo docker exec
  <c> python3 -c "..." > /tmp/cluster_peers_<host>_<container>.json`
- Snapshot the proxy image tag at v5.0.21 as `rollback-batch-3`.

**Rollback:**
- Schema is unchanged (no migration needed); rollback is just the image.
- Restored peer rows from snapshot can be re-inserted via the
  `POST /cluster/peers` admin API once the v5.0.21 image is restored.
- Expected downtime per node during recreate: ~10 seconds; total
  fleet rollback ~2 minutes.

**Retest after batch:**
- **Unit:** existing `test_v5018_cluster_peer_persistence.py` (7 tests)
  + new pins for:
  - prune-on-startup (mock a row whose `id == cluster_node_id`,
    confirm it's removed during lifespan startup)
  - HTTPS URL enforcement (mock POST /cluster/peers with `http://`,
    expect 400 in non-DEBUG mode)
  - `_parse_iso_keep_naive` type-coercion (None on bad input)
- **Integration:** spin up 2 proxy containers, simulate NODE_ID change
  on one, confirm the self-row gets pruned without breaking peer sync
  on the other.
- **Live:** the existing ClusterPeersPanel must continue to add, remove,
  and restore peers via UI. (Playwright E2E for this is a
  coverage-gap recommendation from the QA sweep — see "New test
  recommendations" below.)

**Effort estimate:** 3-4 hours code + 1h tests.

---

### Batch 5 — Statsig validator + cache observability (low risk, UX polish)

**Bugs:** BUG-059 (statsig validator false positives), BUG-060
(statsig cache TTL fixed).

**Why last:** Independent of other batches; both are quality-of-service
improvements (latency reduction); no correctness risk.

**Scope:**
- BUG-059: tighten `_statsig_id_looks_valid` to reject only the
  specific known SDK-error markers (`"x0:TypeError"`, `"x0:Reference"`,
  etc.) rather than the substring `"error"` / `"Error"`. Reduces false
  positive rate from ~0.016% to near 0.
- BUG-060: measure statsig rotation period empirically. Replace the
  fixed 600s TTL with: cache the captured statsig; on any 403 from
  grok.com, mark the cache stale and re-capture. Optionally maintain
  a rolling P95 of "age at first 403" and tune the proactive
  invalidation to 0.7× P95.

**Pre-flight backups:** None required (bridge-only changes; in-memory
state).

**Rollback:** image rollback (~30 seconds).

**Retest after batch:**
- **Validator pin (new):** feed 1000 random base64 strings to
  `_statsig_id_looks_valid` and assert > 99.99% return `True`. Feed
  the captured error fallback values and assert all return `False`.
- **Cache TTL behavior:** instrument the bridge to log statsig age on
  every chat. Run a 6-hour soak; confirm no cached-statsig-cause 403
  occurrences.

**Effort estimate:** 2 hours code + 6h soak.

---

## 3. Cross-cutting recommendations (architectural)

These don't map to specific BUGs but addressing them would prevent
several future BUGs of the same class:

1. **Generic provider response validator** (introduced in Batch 3,
   reusable elsewhere). Apply to every dispatch path so empty-success
   responses always fail loudly.
2. **Bridge pool architecture** (deferred). v5.0.20's SPA-UI driving is
   fundamentally serial through one Chromium tab. For grok-web at
   scale, run N bridge containers behind a sticky-per-conversation
   load balancer. Out of scope for this remediation plan; track as a
   v5.1 backlog item.
3. **Source-pin tests for ALL feature-introducing PRs.** v5.0.18 shipped
   without source pins for the admin-API path; would have caught
   BUG-051. Tighten the PR template to require at least one source-pin
   test for new endpoints.
4. **Compose-file consolidation.** The repo's relationship with
   `/home/dblagbro/docker/docker-compose.yml` is implicit. Move toward
   either (a) the repo OWNS the compose file (and `/home/dblagbro/docker`
   symlinks to it) or (b) the repo never carries a compose file.

---

## 4. New test recommendations (from QA sweep — covers ALL batches)

Specified in `test-plan.md` "Coverage gaps" section but reproduced
here for the per-batch retest cross-reference:

1. **Playwright E2E for ClusterPeersPanel** — add/remove/restore flow.
   Catches future path drift like BUG-051. (Batch 4 dependency)
2. **Bridge `_send_via_spa_ui` concurrency unit test** with mocked
   Playwright `_page`. Catches BUG-052/054/055/058 regressions. (Batch
   2 dependency)
3. **Cursor-bridge error-mapping fixture test** feeding the captured
   `ERROR_RATE_LIMITED_CHANGEABLE` JSON. Catches BUG-053 regressions.
   (Batch 3 dependency)
4. **Cluster-peers 2-node integration test** (full add/remove/restore
   via sync). Catches Batch 4 regressions.
5. **Compose-file ambiguity guard** — pytest fixture asserting
   CWD-resolved `docker-compose.yml` is the canonical one. Catches
   BUG-056 regressions. (Batch 1 dependency)

---

## 5. Sequencing & dependencies

Recommended execution order with parallelism opportunities:

```
Day 1 (low risk):
  Batch 1 (Ops hygiene) — solo, 30 min, no deps.

Day 1-2 (high value):
  Batch 2 (Bridge concurrency) — solo, 5-7h.
  | parallel-safe with: nothing (single bridge sidecar).

Day 2-3 (critical, isolated):
  Batch 3 (Cursor bridge) — solo, 6-8h.
  | parallel-safe with Batch 4 (different subsystems, different
    deploy artifacts).

Day 2-3 (medium risk):
  Batch 4 (Cluster_peers hardening) — solo, 3-4h.
  | parallel-safe with Batch 3.

Day 3 (low risk):
  Batch 5 (Statsig polish) — solo, 2h + 6h soak.
  | parallel-safe with everything.
```

Total: ~24 hours of focused engineering across 3 days, plus 6h
overnight soak for Batch 5.

---

## 6. Pre-flight checklist (before any batch starts)

| Item | Why | How to verify |
|---|---|---|
| Full unit suite passes (2670/2670) | Baseline for regression detection | `python3 -m pytest tests/unit/ -q` shows `2670 passed` |
| Fleet all on v5.0.21 with hotfixes | Known-good state to roll back to | `curl https://www.voipguru.org/llm-proxy{,2}{,-smoke}/health` returns version `5.0.21` |
| `v5.0.21` git tag created on the v2 branch | Easy rollback target | `git tag v5.0.21 <commit>; git push origin v5.0.21` (NOTE: v5.0.18 is the last tag; v5.0.19/20/21 are missing tags — create them) |
| Bridge `:rollback-2026-06-05` image tags exist | Container-level rollback | `sudo docker tag llm-proxy2-grok-bridge:latest llm-proxy2-grok-bridge:rollback-2026-06-05` (and same for cursor-bridge) |
| Cluster_peers tables snapshotted | Data rollback for Batch 4 | JSON dumps in `/tmp/cluster_peers_<host>_<container>_$(date +%s).json` |
| Operator notified of expected fleet recreates per batch | Avoid surprise downtimes (each batch incurs ~30s × N nodes) | Drop a coordinator-post `--working-on "bug-049-068 remediation, batch N"` before each batch deploys |

---

## 7. Rollback decision tree

Each batch has its own rollback target. The decision tree:

```
Did the batch's specific retest fail?
  YES → roll back THIS batch only (image + state per the batch's
        "Rollback" section). Other batches stay.
  NO  → smoke-test the fleet (versions, /health, one chat through
        each cluster).
        FAIL → roll back the most recent batch.
        PASS → proceed to next batch.

Did a previously-passing batch's surface regress after a LATER batch?
  YES → roll back the LATER batch. Most likely Batch 3's response
        validator caught real failures the older code was masking;
        confirm by checking the activity log for new failure events
        before rolling back.
```

---

## 8. Out of scope (explicitly NOT in this remediation plan)

- v5.0.22 feature work (anything not addressing BUG-049..068).
- The Anthropic 1M-context routing decision (v5.0.21 ContextVar
  approach is documented; per-provider opt-out via
  `extra_config.disable_long_context` is the operational interface).
- Grok-bridge concurrency beyond single-tab serialization
  (multi-bridge pool architecture deferred to v5.1 backlog).
- Compliance enforcement (v5.0.0-15 work) — independent.
- The intentional failing-provider fixtures (`C1 Anthropic Claude`,
  `Devin-Codex-Gmail`) per memory note — stay broken.

---

## 9. Operator answers (2026-06-05 PM)

| Q | Answer | Plan adjustment |
|---|---|---|
| 1. Cursor tier | **Free now, will upgrade later — support both** | BUG-053 fix must handle Pro AND Free gracefully (detect tier, route accordingly; don't lock out the provider when downgraded — auto-skip + alert) |
| 2. Bridge concurrency budget | **≤1 confirmed** | Batch 2 is safety-net only; bridge-pool architecture stays in v5.1 backlog |
| 3. cluster_peers backup | **Daily backup pipeline** | Add new line item in Batch 4: integrate cluster_peers JSON dump into the existing nightly backup job (see `docs/backup-plan.md` for the pattern) |
| 4. Stray repo compose | **Rename** (preserve history) | Batch 1: rename to `docker-compose.yml.example.dev` with header explaining what it WAS |
| 5. Grok routing | **NOT OpenRouter-only. Fix grok direct AND add OpenRouter failover** | New Batch 2.5 added below: grok-web → OpenRouter failover wiring. Batch 2 still hardens the bridge as planned (concurrency, listener cleanup) |

---

## 10. Batch 2.5 — Grok-web → OpenRouter failover (new, added 2026-06-05 PM)

Triggered by operator answer to Q5. Goal: when the grok-web bridge fails
(any of: HTTP 599 from SPA-UI errors, HTTP 502 from bridge-unreachable,
HTTP 401/403 still failing after retry), the proxy's routing layer
automatically falls through to OpenRouter's `x-ai/grok-3` model so the
caller sees a real response.

**Scope:**
- Audit `app/routing/router.py` + `app/api/_messages_dispatch.py` for
  how `grok-web` failures currently surface. The bridge returns 599
  (non-standard) for SPA-UI internal errors; verify the router
  recognizes this as failover-eligible (NOT a billing error,
  NOT a permanent revocation).
- If failover doesn't trigger today, add 599 / 502 / 401-after-retry
  to the `is_billing_error` complement set so circuit-breaker
  records a failure and rotation picks the next provider.
- Ensure `OpenRouter-Devin-Personal` (provider id `c477d8a49331`,
  priority 11 on the clone) carries `grok-3` in its routable model
  set. Verify via `select model from provider_models_table where
  provider_id = ?`.
- Add a routing rule: requests for `grok-3` / `grok-2` should have
  Grok-Web-Devin first (low-cost path) AND OpenRouter as the next
  fallback in the chain.

**Pre-flight backups:**
- Snapshot `Grok-Web-Devin` + `OpenRouter-Devin-Personal` provider rows
  on both clusters (extra_config, priority, enabled, auto_skip_until,
  manual_override_*).
- Tag the current proxy image.

**Rollback:**
- Restore provider rows from JSON snapshot.
- Roll back the proxy image.

**Retest after batch:**
- **Failure injection:** deliberately break the grok-bridge container
  (`sudo docker stop llm-proxy2-grok-bridge`). Fire a `grok-3` request
  through the proxy. Acceptance: response returns from OpenRouter with
  real content, response headers carry the proxy's failover indicator,
  activity log shows two entries (grok-web 599 → fallthrough →
  openrouter 200).
- Restore bridge: chats should route to grok-web again (the cheaper
  default).
- Verify the SAME behavior on the clone cluster.

**Effort estimate:** 2-3h.

**Sequencing:** Run after Batch 2 (the bridge fix may eliminate some
of the 599 sources we'd be relying on for failover trigger). Can run
in parallel with Batches 3 + 4.

---

## 11. New finding from DevinGPT-team reply — BUG-069

DevinGPT team reported that the OLD pre-migration key (`llmp-XBM_JyE…`)
works on `/llm-proxy/` clone despite the dual-cluster memo claiming
keys aren't cluster-synced. Investigation 2026-06-05 PM:

The clone DB has **18 keys inherited from the snapshot-and-fork** at
2026-06-05 11:39 EDT, plus the 1 new DevinGPT key provisioned today
(`llmp-up3OUnImc`). State diff vs original `/llm-proxy2/`:

| Key | `/llm-proxy2/` original | `/llm-proxy/` clone | Drift |
|---|---|---|---|
| `llmp-XBM_JyE` (devinGPT) | enabled=False | enabled=True | YES |
| `llmp-Ut3S3FU` (paperless) | enabled=False | enabled=True | YES |
| `llmp-2Hj` (tax-ai-analyzer) | enabled=False | enabled=True | YES |
| `llmp-arc` (archa-harness) | enabled=False | enabled=True | YES |
| ~5 others | enabled=False | enabled=True | YES |

**Root cause:** the operator disabled these keys on `/llm-proxy2/`
AFTER the snapshot fork; the clone retained their pre-disabled enabled
state.

### BUG-069 — Clone inherits pre-fork api_keys; state drift on intentional disable
- **Component:** `cluster_peers` table seed flow + dual-cluster memo
  accuracy.
- **Severity:** **high** (operator-disabled keys still work on the
  clone — potential security gap for any key whose disable was
  security-motivated; for DevinGPT's case the impact is benign per
  the team's reply).
- **Repro:** `sudo docker exec llm-proxy python3 -c "select prefix,
  enabled from api_keys"` shows ~8 keys enabled=True; same query on
  `llm-proxy2` shows enabled=False. Same key prefixes, different
  state.
- **Fix:** Two complementary actions:
  1. **One-shot reconciliation:** decide per-key whether the
     intentional disable on `/llm-proxy2/` should also apply on the
     clone. Most likely YES for security-motivated disables (revoked
     test keys, decommissioned project keys); NO for compliance-
     motivated disables (the clone has the "full options" posture
     where these keys SHOULD be usable). Operator to triage.
  2. **Documentation:** Update the dual-cluster memo + the
     `project_llm_proxy_dual_cluster` memory note to explicitly state
     that the clone inherits the api_keys table from the fork moment
     and key state changes do NOT propagate to / from the clone.
- **Status:** open. Drop into Batch 4 (cluster-peers / cluster hygiene
  hardening), since it's adjacent to the cluster_peers documentation
  work already there.

This BUG joins Batch 4. The reconciliation step is operator-driven
(triage call per key).

---

End of plan v2. Awaiting operator approval to start Batch 1.
