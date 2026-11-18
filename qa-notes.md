# QA Notes — llm-proxy-v2

Operational quirks, environment assumptions, flaky behavior, and risk
notes accumulated during regression sweeps. Update freely; this file is
deliberately less structured than `bug-log.md` or `test-plan.md`.

## Post-refactor regression sweep (2026-06-12 — v5.1.0 → v5.3.9)

Findings BUG-069..BUG-075 in `bug-log.md`. No hotfixes shipped during
this sweep (no defects severe enough to gate the v5.3.9 release tag).

- **The release-tag-then-QA pattern is the new failure mode.** v5.3.x
  shipped *eight* point releases in a week (5.3.0 → 5.3.9) under
  operator urgency around a possible code freeze; the entire v5.2
  vendor-neutrality stack and v5.3 supervisor / CB hardening went out
  with per-release unit pins but no consolidated regression sweep
  until this one. Unit baseline grew from 2670 → 2910 (+240) without
  intermediate QA. **Mitigation already-in-place:** the 2026-06-08
  daily fleet-health routine catches version skew within 24h; this
  sweep gives it the first ground-truth snapshot to compare against.
- **All four high/medium findings cluster around observability, not
  correctness.** v5.3.9 ships clean — the unit suite is green, the
  classifier behaves, the fleet is on parity. What's missing is the
  ability to *see* whether the proxy is doing what we shipped. BUG-069
  (worker heartbeat), BUG-070 (supervisor silent), BUG-071 (policy
  not exercised), BUG-072 (retry tap silent), BUG-073 (audit chain
  zero-row signing), BUG-074 (cluster sync silent), BUG-075 (no
  dbPool in /health). One worker-heartbeat refactor (BoolSystemSetting
  pattern applied to a `WorkerHeartbeat` factory) closes ≥4 of these.
- **Coordinator-hub key has had no compliance policy for the entire
  v5.2 lifetime.** The v5.2 vendor-neutrality work is the largest
  feature shipped in 2026 so far; its dominant production caller
  generates ~all the audit-trail signal that proves the subsystem
  works. With all four policy columns NULL on the coordinator-hub
  key, the subsystem has effectively been in dry-run mode against
  prod traffic since v5.2 cut. This is an operator policy decision,
  not a code defect — but the audit-chain dutifully signing daily
  zero-row windows reads as theatre on inspection.
- **The AI supervisor enablement / auto_apply / fixture-noise Tier A
  (2026-06-11) presumed the supervisor was already running.** It
  isn't — zero `supervisor_*` activity-log rows in 7 days on tmrwww01.
  The v5.3.9 CB hardening (caller-side classifier + auto-probe + hysteresis)
  is doing the right thing in isolation but its self-healing partner
  is silent. BUG-070 needs to be diagnosed before claiming the
  fleet is properly self-healing.
- **The intentional failing-provider fixtures continue to dominate
  the snapshot.** C1 Anthropic Claude (priority 112) is OPEN on
  tmrwww01 at sweep time, as expected; the v5.3.9 hardening should
  prevent it from tripping its now-`failure_threshold = 1_000_000`
  fixture-mode peers. Resist any urge to "investigate" it.

## Post-refactor deep regression sweep (2026-06-05 PM — v5.0.21)

Findings BUG-049..BUG-068 in `bug-log.md`. Hotfixes shipped during sweep:
v5.0.21 (defensive getattr + identity-check bool, `test_v5021_…`) and
v5.0.18-hotfix (frontend `/api/cluster/peers` → `/cluster/peers`).

- **The bug-log-lag pattern recurred AGAIN.** This time spanning the
  v3.x → v4.x → v5.x lineage (~22 months since the last QA pass)
  during which the entire compliance enforcement subsystem, the airi
  feature, LMRH v2, claude-oauth, the messages.py split, the cursor
  bridge, the grok bridge SPA-UI refactor, and the cluster-peers
  feature all shipped. Test-plan baseline went from 1973 → 2670 tests
  with no intermediate sweep. **Mitigation:** schedule a QA sweep
  every ~5 versions or every 2 weeks, whichever comes first.

- **Deploy ambiguity from stray repo-root `docker-compose.yml`.** BUG-056
  cost me >1h during the sweep. Three times I shipped a hotfix and the
  clone container didn't pick it up because `cd /home/dblagbro/llm-proxy-v2`
  was implicit when running `git commit` and the followup `sudo docker
  compose up …` resolved to the repo's compose file which only knows
  about `llm-proxy2`. Always `cd /home/dblagbro/docker` before any
  `docker compose` command; better yet, delete or rename the repo-root
  one.

- **Grok bridge concurrency is fundamentally bottlenecked by Playwright
  single-tab.** v5.0.20 SPA-UI driving works for sequential requests
  but cannot serve 2 concurrent chats safely. Production routing
  through grok-web at scale will require either (a) explicit
  per-conversation queueing, (b) a pool of bridge sidecar containers,
  or (c) acceptance that grok-web is for ad-hoc / low-volume use only.
  OpenRouter remains the right Grok routing target for production.

- **Cursor account state degrades silently.** BUG-053 surfaced because
  the Cursor billing scrape (per-memory project `cursor-oauth usage
  monitoring + multi-account preferred-pick`) tracks usage utilization
  but NOT plan tier. A Pro→Free downgrade leaves the scraper happy
  (usage stays at 0%) while every routing request returns empty. Add
  a plan-tier field to the scrape + alert on changes.

- **ContextVar + asyncio Task scoping is correct but subtle.** The
  v5.0.21 design relies on FastAPI creating a fresh asyncio Task per
  request so the ContextVar value set by one request can't bleed into
  another. This holds for the streaming dispatch sites I checked, but
  a single test runner sharing a Task across multiple async-fixture
  invocations would leak the value. Pin tests in
  `test_v5021_disable_long_context.py` explicitly reset
  `_disable_long_context_cv.set(False)` after each test to prevent
  intra-suite leakage.

- **The intentional failing-provider fixtures kept tripping me up.**
  `C1 Anthropic Claude` (priority 112) is intentionally broken per
  the memory note `reference_intentional_failing_provider_fixtures.md`.
  When my chat tests returned empty content with model `claude-4-sonnet`,
  my first hypothesis was the failing fixture. It turned out to be
  Cursor-OAuth's empty-response masking (BUG-053). Lesson: ALSO check
  which provider was actually selected via the activity log before
  blaming a known-broken fixture.

## Post-refactor deep regression sweep (2026-05-15 PM — v3.10.9)

Deep sweep covering **v3.9.16 → v3.10.9** — 14 releases shipped since
the last QA pass. Findings BUG-023..BUG-036 in `bug-log.md`.

- **The bug-log-lag pattern recurred.** Same lesson as the v3.9.15
  audit: 14 releases (translation fix, severity taxonomy, ARCH-A
  toolkit, error-rate alert, supervisor enablement, LMRH v2, the
  `messages.py` refactor) shipped with no QA pass. `test-plan.md` had
  drifted to a 633-test baseline (actual: 1969). Tighten: a QA sweep
  per ~milestone, not per dozen releases.
- **ARCH-A is no longer just latent — it is actively manifesting.**
  www01 logs show 7× `sqlalchemy.pool` connection-GC / "Connection
  closed" errors in a 3h window. Tracer (`DB_POOL_TRACE=1`) is now ON
  for all 3 nodes — GCP added during the v3.10.10 deploy.
- **`test_revoke_key_rejects_llm_calls` failure — BUG-023 retracted
  (v3.10.10).** The sweep filed this as "a revoked key still
  authenticates." Re-investigation **disproved** that: a direct probe
  of a soft-deleted key (`enabled=False`, `deleted_at` set) returns
  **401 in 0.0s** on both `/v1/models` and `/v1/messages` — the
  revocation path is correct. The test's failure was a **read
  timeout**: it used `claude-3-5-sonnet-20241022`, a model with no
  capability rows on this cluster, whose dispatch can hang ~40s+
  (BUG-037). Lesson: an "Actual: HTTP 200" line in a bug report must
  be an *observed* status, not an inference from a test failure — a
  timeout failure is not evidence of an auth bypass. The test now uses
  a registered model. `verify_api_key` still gained a
  `deleted_at IS NULL` filter as genuine defence-in-depth.
- **Cluster-sync resurrects out-of-band hard deletes.** Scripts /
  harnesses that `db.delete()` a row directly (including
  `scripts/archa_pool_leak_harness.py`'s cleanup `finally`) have their
  temp keys re-inserted by a peer and left live. Scripts must use the
  tombstone path (`deleted_at` + `enabled=False`), not `db.delete()`.
- **Non-repo config drift** — www01's compose carries `DB_POOL_TRACE`,
  `AI_PROVIDER_SUPERVISOR_ENABLED`, `AI_PROVIDER_SUPERVISOR_INTERNAL_API_KEY`,
  `LMRH_V2_NODE_OVERRIDE`; www02 has `DB_POOL_TRACE`; GCP has none.
  These are operational flags absent from git — a fresh deploy
  elsewhere would not have them. Documented in the v3.10.8 pause-state
  memory.
- **AI supervisor is enabled suggest-only on www01** — its classifier
  self-calls carry `X-Internal-Source: ai_provider_supervisor` but
  **nothing reads that header** (BUG-026): the supervisor counts its
  own traffic in the stats that drive its verdicts.
- **Environment constraint**: this sweep ran with no browser —
  interactive Playwright UI testing was NOT exercised. UI validation
  was limited to `tsc` + code/wiring inspection + the API behind it.
  Declared coverage gap GAP-7.
- `messages.py` is 816 lines post-v3.10.9-refactor — still over
  `design.md`'s 800 trigger; the CoT/litellm dispatch-tail extraction
  is the named next refactor.

## Audit refresh (2026-05-15)

Re-checked the 2026-04-24 sweep against current code. 16 of 18 items
were addressed by intermediate versions without updating bug-log.md;
bug-log.md now has the reconciled statuses. Lessons learned:

- **The bug-log lagged behind the code by ~3 weeks.** When fixes ship,
  the fixer should write a one-line `Status: verified-fixed in vX.Y.Z`
  entry the same session — don't trust that "the commit log is the
  bug log". Tighten the release-ceremony checklist.
- **The recurring patterns this sweep flagged were mostly already
  resolved**: hardcoded version strings (BUG-004/BUG-013), provider
  auth-error lifecycle (BUG-002/BUG-003/BUG-008), comma-list filters
  (BUG-014), DB indexes (BUG-017). All shipped between v2.7.6 and
  v3.7.16.
- **One thing the audit DID find new**: a latent DB connection leak
  surfaced today on www01 and GCP (13h and 20h to saturate the pool
  post-deploy). Every `AsyncSessionLocal()` is `async with`-wrapped
  per the audit, so the leak isn't naive session-leak. Filed as
  ARCH-A in bug-log.md with diagnostic plan for next recurrence.

## Now-current "things to add later" list

- [x] Single-source version (`app/__version__.py`) — done
- [x] `is_auth_error()` classifier — done in `circuit_breaker.py`
- [x] 401 handler in claude-oauth dispatch — done in `_messages_streaming.py`
- [x] Background refresh job — done (token-refresh on 401)
- [x] DB indexes on hot lookup columns — done in `database.py`
- [x] "Needs re-auth" provider UI badge — done (manual_override + ai_supervisor)
- [x] `--skip-destructive` flag on burn test — done v3.9.15 (BUG-012)
- [ ] Streaming-error contract redesign (BUG-001) — deferred pending
      DevinGPT/hub design sign-off
- [ ] Pool-leak investigation (ARCH-A) — needs next recurrence to
      localize; mitigations in place to detect (dbPool gauge + Prometheus)

## Environment assumptions (2026-04-24)

- Production cluster: tmrwww01 (primary, this host) + tmrwww02 + GCP node `c1conversations-avaya-01-s23`. All on v2.7.5 as of this sweep.
- Admin password used by the integration suite is hardcoded as `<redacted — see LLMPROXY_TEST_ADMIN_PASS>` in `tests/conftest.py`. README still says `admin/admin` (BUG-009).
- Default DB path inside the container: `/app/data/llmproxy.db`. Volume: `docker_llm-proxy2-data`.
- Frontend assets at `/llm-proxy2/assets/index-<hash>.js`. Hash changes with each rebuild; index.html has no `Cache-Control` header (BUG-015 — minor).

## Provider state on this cluster (snapshot)

All non-OAuth provider keys are missing or truncated (probably an artifact of an earlier reset). Concretely:
- `Anthropic Claude Code #3` (priority 1): `api_key` length **11** chars (`sk-ant-a...` truncated). Returns `invalid x-api-key` on every test.
- `C1 Anthropic Claude` (priority 2): same — truncated.
- 4× Google providers: `api_key` is the empty string. litellm complains `Missing GEMINI_API_KEY` env var.
- `Devin Personal OpenAI ChatGPT`: empty key.
- 2× mock providers: have keys, work for local mock loop.
- `Devin-VG` (claude-oauth): had a valid token at v2.7.5 deploy, has since been revoked server-side by Anthropic (token expiry ~14k seconds in the future, but Anthropic returned 401 anyway). Refresh-token from initial OAuth was consumed by an earlier non-persisting test.

**Operational implication**: Almost every test that goes "all the way to a real upstream" will return errors. This isn't a code regression — it's that the cluster is operating with empty/expired credentials on every non-OAuth provider. Several test failures fall out of this state and will resolve once the keys are re-paste.

## Flaky / time-sensitive tests

- `tests/integration/test_routing_mock.py::TestToolEmulation::test_plain_text_when_no_tool_call_in_response` flapped 502→PASS during this sweep — likely a transient when a provider was mid-restart or the mock fixture was racing.
- Prompt-caching live tests need **≥3 seconds** between requests for cache propagation; 1 second is too short.
- Rate-limit window is RPM (per-minute), so consecutive test runs need ~60s of bleed between them. The integration suite already paces, but ad-hoc shell loops will see false 429s.

## Activity-log filter quirk

`?severity=warning,error` does a literal-string match against the column rather than splitting on `,`. Workaround: issue separate calls per severity. Real fix in BUG-014.

## OpenAPI surface

- 53 paths, all have operationId. No obvious schema breakage.
- Spec is auto-generated; there's no separate `openapi.yaml` to keep in sync.

## Fragility patterns we keep seeing

1. **Hardcoded version strings in tests** — every release someone forgets to bump the test (BUG-004). Recommend single-source-of-truth via `app/__version__.py` (BUG-013).
2. **Two providers tied at priority=1** with no warning (BUG-010) — operators shouldn't be able to silently get non-deterministic routing.
3. **Auth errors classified as transient** — circuit breaker resets after hold-down and re-tries the same broken key (BUG-002 / BUG-003). Belongs in `is_billing_error`-style classifier as a sibling.
4. **claude-oauth dispatch path short-circuits the fallback chain** — by design (per code comment) but a single token revocation translates straight to user-facing errors (BUG-008).
5. **SSE error events with HTTP 200** — a recurring pattern. Streaming code path appears to catch most provider errors and emit them as terminal SSE frames; clients interpreting the stream see "success but empty" (BUG-001).

## Recurring "this isn't a bug, but..." observations

- `oauth_expires_at` is treated as authoritative locally; Anthropic can revoke earlier. Don't trust the local clock.
- Refresh tokens are single-use and rotated. **Do not** call `refresh_access_token()` directly from anywhere outside `refresh_and_persist()` (BUG-007).
- The Claude Code system marker MUST be the first system block; if a future feature tries to inject something before it (e.g., privacy filter), the OAuth path will start returning the masked `rate_limit_error`.
- Haiku at the Pro Max tier doesn't get 1M context — `_beta_flags_for_model` strips that flag. Adding new Pro Max-restricted flags should follow the same pattern.

## Retest cadence recommendations

| Test | Frequency |
|---|---|
| `python3 -m pytest tests/unit/` | every commit |
| Non-Playwright integration | every deploy, every node |
| Playwright UI | every deploy to www1 |
| `scripts/test_claude_oauth_live.py` | once per OAuth-touching change AND weekly to catch token-revocation drift |
| Schema migration audit (`PRAGMA table_info`) | once per schema PR |
| Provider key audit (test all enabled providers) | weekly |

## Things to be added later

- Single-source version (`app/__version__.py`) and update everywhere
- `is_auth_error()` classifier in `circuit_breaker.py`
- 401-handler in claude-oauth dispatch with `refresh_and_persist`
- Background job to refresh tokens approaching expiry
- DB indexes on hot lookup columns (api_keys.key_hash, activity_log.timestamp/provider_id, provider_metrics.(provider_id, bucket_ts))
- A "needs re-auth" pill in the Providers UI when a claude-oauth provider's last error is 401 / `invalid_grant`
- Weekly automated `scripts/test_claude_oauth_live.py` run via a non-destructive flag (`--skip-refresh`) — requires BUG-012

---

# Archived: operational QA notes (formerly `docs/qa-notes.md`)

Merged verbatim on 2026-11-18 from `docs/qa-notes.md`, the QA-pass observation ledger
(v3.5.7 to v4.4.x, 2026-05-09 to 2026-05-28). Sections keep their original order and dates.
References elsewhere to "`docs/qa-notes.md` §..." mean the sections below.

Operational observations + environmental quirks + things-you-should-know-when-debugging that don't fit cleanly in `bug-log.md` or `test-plan.md`.

Created 2026-05-09 during the deep QA pass. Append-only ledger.

---

## 2026-05-27 — Deep QA pass operational observations

### Cluster heartbeat health is misleading when apply_sync is broken

`/cluster/status` reports peers as `status=healthy` based on a separate heartbeat path (`_ping_peer` in `app/cluster/manager.py`). The heartbeat exercises a different code path than `apply_sync`. Until BUG-079 is fixed, **never assume heartbeat = data sync working**. To verify actual cluster sync is functional, mint a test row + observe its propagation to peers' DBs (or check apply_sync status in peer logs).

### `push_sync` swallows peer non-200 responses

Per BUG-081 — `app/cluster/manager.py:518` calls `await client.post(...)` and ignores the response. Network exceptions log; HTTP errors don't. This combined with the heartbeat-health blind spot is why BUG-079 went ~6 days undetected. When debugging cluster issues, **inspect peer-side `/cluster/sync` logs directly** rather than trusting the originating node's logs.

### Cron auto-watchers — how they self-suppress

Three cron entries armed during this session:
- `/home/dblagbro/bin/f2_cache_verify.sh` (every 30 min)
- `/home/dblagbro/bin/devingpt_header_verify.sh` (every 15 min)
- `/home/dblagbro/bin/c1conv_v4423_retry.sh` (every 30 min — already self-suppressed)

Each writes a `.done` sentinel to `/home/dblagbro/log/<name>.done` once its verdict fires; subsequent invocations early-exit when the sentinel exists. To **re-arm** any of them: `rm /home/dblagbro/log/<name>.done`. Verdict text lives in `/home/dblagbro/log/<name>.verdict`.

### Provider AI Review duplicate rows — diagnosing

Quick check from any node's container:
```python
import sqlite3
c = sqlite3.connect('/app/data/llmproxy.db').cursor()
for r in c.execute("SELECT provider_id, captured_at, COUNT(*) AS n FROM provider_ai_review GROUP BY provider_id, captured_at HAVING n > 1").fetchall():
    print(r)
```
A non-empty result on a peer = `apply_sync` from www1 (or any peer) will 500 on that peer. Same pattern applicable to `_apply_blocked_ips` / `_apply_ai_reviews` / `_apply_caller_memory*` if duplicates ever occur (BUG-080).

### Test-infra: pytest sessionfinish hits live production

Per F-INFRA-001 — running `pytest tests/unit/` from a clean checkout still POSTs to `https://www.voipguru.org/llm-proxy2/api/keys/_purge-test-tombstones` at the end. Affects CI portability. The unit suite is NOT hermetic in its current shape.

### Frontend contrast: `text-gray-400` on light backgrounds

Per F-OBS-004. Stat-card labels and table helper text use `text-gray-400` which fails WCAG AA on white. Not a UX blocker but a real readability issue for low-vision operators.

### `gcloud compute ssh` to c1conv is flaky under CPU pressure

Documented previously in `reference_avaya01_ssh_hang_is_overload.md`, observed again 2026-05-27 — three back-to-back failures on the v4.4.23 deploy attempt. The auto-retry script (`/home/dblagbro/bin/c1conv_v4423_retry.sh`) caught up automatically within minutes. For routine deploys, set up the cron retry path BEFORE attempting the manual hop.

## 2026-05-28 — v4.4.27 migration observations (UNIQUE constraint shipped)

### The race that wrote BUG-079's duplicate is real and live

Between v4.4.24's de-dup on www2 (2026-05-27, 1 dup → 0) and v4.4.27 prep (2026-05-28), www2 silently accumulated **3 NEW duplicate groups in ~24 hours**. The `.limit(1)` guard prevented apply_sync from crashing on these new dupes, but the underlying check-then-insert race kept writing them. This validates the v4.4.27 work: without the UNIQUE index, dupes accumulate forever (operationally invisible until the next apply_sync without a `.limit(1)` partner crashes again).

### Idempotent migration pattern is reusable

The v4.4.27 init_db block (de-dup via ROW_NUMBER + ranked keeper heuristic, then CREATE UNIQUE INDEX IF NOT EXISTS) is the template for any future `(col_a, col_b)` UNIQUE constraint we want to add to a SQLite table that may already have duplicates. It runs every container start with zero overhead once the table is clean. Reusable in `app/models/database.py::init_db`.

### Keeper heuristic — pick the row carrying lifecycle fields

When de-duping `provider_ai_review` / `api_key_ai_review`, prefer rows where any of `applied_at`/`dismissed_at`/`reverted_at` is non-NULL (operator action carried). Fall back to highest id for ties. Generic enough to mirror to any table with similar lifecycle columns.

### v4.4.28 contrast sweep — `text-gray-500` `dark:` variant scope

Tailwind `text-gray-500` on its own works fine in light mode (~4.83:1 on white) but fails AA in dark mode (~3.03:1 on `bg-gray-900`). The `dark:text-gray-400` variant fixes dark without disturbing light. Sweep across 29 files used `perl -pe 's/\btext-gray-500\b(?! dark:text-gray-)/text-gray-500 dark:text-gray-400/g'` — the negative lookahead skips elements that already have a `dark:text-gray-` companion (avoids double-darks). Re-runnable safely.



---

## 2026-05-12 — Querying cluster-replicated tables (operator/agent runbook)

### The trap

Tables that participate in `/cluster/sync` (currently `external_usage_snapshot`, `ai_review`, `blocked_ips`) get rows replicated from peer nodes with the **same `captured_at` / timestamp** as the originating row. A naive "give me the latest per provider" query like

```python
# ❌ DON'T — non-deterministic on cluster-replicated tables
q = (
    select(ExternalUsageSnapshot)
    .order_by(ExternalUsageSnapshot.captured_at.desc())
    .limit(10)
)
seen = set()
for row in (await s.execute(q)).scalars():
    if row.provider_id in seen:
        continue
    seen.add(row.provider_id)
    yield row
```

silently returns the wrong row when two siblings tie on `captured_at` — SQLA emits whichever happens first, and "first" is replica-vs-originator non-deterministic. This bit our self-monitor twice on 2026-05-12 (07:07 EDT and 10:27 EDT), reporting "worker stalled" while docker logs showed it firing on schedule.

### The fix

Use `MAX(captured_at) GROUP BY provider_id` as a subquery and join back:

```python
# ✅ DO — deterministic latest-per-provider
from sqlalchemy import select, func

latest = (
    select(
        ExternalUsageSnapshot.provider_id,
        func.max(ExternalUsageSnapshot.captured_at).label("mx"),
    )
    .group_by(ExternalUsageSnapshot.provider_id)
    .subquery()
)

q = (
    select(
        Provider.name,
        ExternalUsageSnapshot.captured_at,
        ExternalUsageSnapshot.seven_day_utilization,
        ExternalUsageSnapshot.auth_state,
    )
    .join(Provider, Provider.id == ExternalUsageSnapshot.provider_id)
    .join(
        latest,
        (latest.c.provider_id == ExternalUsageSnapshot.provider_id)
        & (latest.c.mx == ExternalUsageSnapshot.captured_at),
    )
)
```

If two rows still tie after the join (e.g. originator + replica with identical `captured_at`), add a tiebreaker like `.order_by(ExternalUsageSnapshot.id.desc()).limit(1)` per provider — but that's normally unnecessary; SQLA returns one row per provider via the join key.

### Applies to

- `external_usage_snapshot` — cluster-synced since v3.7.15
- `ai_review` — cluster-synced since v3.7.15
- `blocked_ips` — cluster-synced since v3.7.15 (use tombstone column `deleted_at IS NULL` filter)

For non-replicated tables (e.g. local-only `activity_log`), the naive `ORDER BY + LIMIT + seen-set` pattern is fine — but get in the habit of using the GROUP BY pattern uniformly; it's the same effort and immune to future replication.

---

## 2026-05-10 — QA pass v3.7.13 / v3.7.14 observations

### Admin auth: `require_admin` accepts SESSION TOKENS ONLY, not admin API keys

The `_extract_token()` helper in `app/auth/admin.py` reads the
`llmproxy_session` cookie OR the `Authorization: Bearer <token>`
header — but **the token must be a session token from the `sessions`
table**, not an API key from `api_keys`. Creating an `ApiKey` row
with `key_type='admin'` and trying to use its `llmp-...` value
returns 401 "Session expired or invalid" because `_get_session()`
finds no matching `sessions.token`.

To programmatically obtain an admin session for testing:
```python
from app.auth.admin import create_session
token = await create_session(user_id, username, role)
# token now valid as Authorization: Bearer <token>
```

The default credentials in CLAUDE.md (`admin`/`admin`) only work on
**first boot when no users exist**. Once any User row exists, the
default-admin seed is skipped — the actual admin password must be
known to use the login flow.

### Sessions table: `created_at` / `last_seen_at` are FLOAT (unix epoch)

`app/models/db.py:Session` stores timestamps as `Float` (not
`DateTime`). Inserting a `datetime.datetime` value into them via
SQLAlchemy ORM raises `TypeError: float() argument must be a string
or a real number, not 'datetime.datetime'` deep inside the SQLA
processor. Use `time.time()` directly, or use
`app.auth.admin.create_session()` which handles this correctly.

This caught me building a session manually — the helpful path is
always to use `create_session()`.

### IP block: an operator who blocks their own IP can lock themselves out (BUG-019)

The v3.7.11 IP block middleware runs at the very front of the
ASGI stack (correctly — it short-circuits expensive work for
blocked traffic). But it had no recovery exemption. v3.7.14 added
narrow exemptions for `/api/auth/login` and `/api/admin/blocked-ips`
so an operator can always sign in and remove block entries even
from a blocked IP.

If you see "Source IP is blocked by administrator." while you have
admin credentials: hit `DELETE /api/admin/blocked-ips/<your-ip>`
and you'll get through. If you're on a pre-v3.7.14 build (shouldn't
happen post-2026-05-10), direct DB recovery is:
```python
from sqlalchemy import delete
from app.models.db import BlockedIp
await db.execute(delete(BlockedIp).where(BlockedIp.ip == '<ip>'))
await db.commit()
```
Then restart the container to flush the 30s in-memory cache.

### Compose-on-peer: SSH command working directory is `/home/dblagbro`, not `/home/dblagbro/docker`

When deploying a single container to a peer via SSH, **do not** rely
on `cd /home/dblagbro/docker &&` — the Bash tool here strips `cd`
prefixes from commands by policy. Instead use `-f` + `--project-directory`:
```bash
ssh tmrwww02 'sudo docker compose \
  -f /home/dblagbro/docker/docker-compose.yml \
  --project-directory /home/dblagbro/docker \
  up -d --force-recreate --no-deps llm-proxy2'
```
Without `--project-directory`, relative paths in the compose file
(volumes, env_file, build contexts) resolve against the SSH default
home directory and break the build. This bit me twice during
v3.7.14 deploy.

### Cluster sync gap: new v3.7.x tables not auto-syncing (BUG-016)

When adding tables that should be cluster-replicated, they must be
explicitly added to the cluster-sync allowlist in
`app/cluster/sync.py`. The three v3.7.x tables that landed without
sync entries are documented in BUG-016. Pattern: new schema files
should add an `# CLUSTER-SYNC: yes/no` comment near the class
definition to make the intent explicit.

---

## 2026-05-09 — Initial QA pass observations

### Auth header conventions (do NOT confuse them)

- **`/v1/messages`**: requires `x-api-key: llmp-...` (Anthropic spec convention). `Authorization: Bearer ...` returns HTTP 401 "Missing API key". This is correct per Anthropic's API spec.
- **`/v1/chat/completions`**: accepts `Authorization: Bearer llmp-...` (OpenAI spec convention).
- **`/api/monitoring/*` admin endpoints**: require an admin SESSION cookie, not an API key. Bearer token returns 401 "Session expired or invalid". Use the admin login UI to get a session.
- **`/lmrh/*` endpoints**: API key (Bearer or x-api-key both accepted).
- **Symptom of confusion**: requesting `/v1/messages` with `Authorization: Bearer` returns 401 "Missing API key". I fell into this trap during T4 of the QA pass. The 401 is correct but misleading — there is an API key, just in the wrong header.

### Test contamination of production DB

The integration tests in `tests/integration/` hit the LIVE deployed proxy (`https://www.voipguru.org/llm-proxy2/`) by design — that's documented in CLAUDE.md. They create + soft-delete `pytest-mock` provider rows.

Consequence: every integration test run leaves cruft in:
1. `providers` table (rows with `deleted_at` set)
2. Circuit breaker `_local_states` dict (orphan entries — see BUG-012)
3. Activity log (test traffic mixed with real)

Before reasoning about provider state from a `/health` snapshot, **filter for `name NOT LIKE 'pytest%' AND deleted_at IS NULL`**. Otherwise you'll see ghost providers.

### Live HTTPS endpoint reference

- Production fleet: `https://www.voipguru.org/llm-proxy2/` (www01) and `https://www2.voipguru.org/llm-proxy2/` (www02)
- Smoke: `https://www.voipguru.org/llm-proxy2-smoke/`
- GCP: `https://34.170.189.19/llm-proxy2/` (self-signed cert; `curl -k` required)
- Bridge sidecar: `https://www.voipguru.org/grok-bridge/api/status` (token-gated)
- Test API key (in memory `feedback_paperless_token_burn`): `llmp-<redacted — v5.22.15; key was dead, but a public repo is no place for a key value. Fetch from the Keys page if you need it.>` (key=grok-web-smoke)

### Cluster behavior — what's per-node vs cluster-replicated

Per `docs/architecture.md`:

- **Per-node** (each cluster member computes independently):
  - `activity_log` rows
  - `provider_metrics` 5-min buckets
  - `_local_states` for circuit breakers
  - LMRHv2 snapshot ETags (consequence: callers polling via load-balancer see ETag drift even when config is identical — BUG-011)
- **Cluster-replicated** (LWW with last_user_edit_at tie-break):
  - `providers` table
  - `model_capabilities` (with v3.4.1+ aliases / v3.5.0+ family/variant)
  - `model_aliases` table
  - `lmrh_dims` + `lmrh_proposals`
  - `system_settings` (the runtime-tunable settings)

### Performance observations from T-pass

- `/health` average response: ~9ms. Excellent.
- `/v1/models` with auth: ~50-200ms (depends on whether snapshot is fresh)
- `/lmrh/providers` 304 Not-Modified round-trip: ~30ms (well under the 60s recommended polling interval)
- LMRH per-key rate limit: **4 req/min** for `/lmrh/providers`. Burst of 10 → 5 succeed, 5 get 429 with `Retry-After`. The 4/min is intentionally low; SSE subscribe is the recommended path for high-volume callers.
- `/v1/chat/completions` with grok-3 via Grok-Web: 4-5s avg latency, 33% rate-limit hit rate (per the 2026-05-09 PM Grok eval bench)
- `/v1/chat/completions` with grok-4: 10s avg latency, 5x output-token bloat from reasoning trace

### Common debugging commands

```bash
# Inspect probe back-off state (admin endpoint, v3.5.4+)
curl -sk -H "Cookie: session=$ADMIN_SESSION" https://www.voipguru.org/llm-proxy2/api/monitoring/probe-state

# Or via docker exec (no admin session needed):
sudo docker exec llm-proxy2 python3 -c "
from app.monitoring.keepalive import get_backoff_state
print(get_backoff_state())
"

# Inspect circuit breaker state for all providers
sudo docker exec llm-proxy2 python3 -c "
from app.routing.circuit_breaker import get_all_states
import json; print(json.dumps(get_all_states(), indent=2))
"

# Live SSE smoke
curl -sk -N -H "Authorization: Bearer $KEY" \
  "https://www.voipguru.org/llm-proxy2/lmrh/stream?heartbeat_sec=10" -m 8

# Activity log query (note: created_at is SPACE-separated, not ISO-T)
# See reference_llm_proxy2_db_query_gotchas.md for full gotchas
sudo docker exec llm-proxy2 python3 -c "
import sqlite3
from datetime import datetime, timedelta
db = sqlite3.connect('/app/data/llmproxy.db')
db.row_factory = sqlite3.Row
cutoff = str(datetime.utcnow() - timedelta(hours=1))  # MUST be str(), not isoformat()
for r in db.execute('SELECT severity, event_type, COUNT(*) c FROM activity_log WHERE created_at >= ? GROUP BY severity, event_type', (cutoff,)):
    print(f'{r[\"severity\"]:8} {r[\"event_type\"]:20} {r[\"c\"]}')
"
```

### Environment quirks worth knowing

1. **Frontend type-check is silent on success** — `npx tsc --noEmit` returns nothing when clean; don't interpret empty stdout as a failure.
2. **Container clock is UTC** — host is EDT. `datetime.utcnow()` inside the container matches the activity_log timestamps.
3. **SQLite WAL mode is on** — readers may see slightly stale snapshots; if a query result looks impossible (e.g. all `enabled=0`), re-run before declaring it a bug. Saw this during the QA pass; first query returned all-disabled, second returned all-enabled with no DB modification between them.
4. **OpenRouter fallback rate** — Grok models route through Grok-Web (priority 1) by default. To force OpenRouter for testing, send `LLM-Hint: exclude=grok-web`.
5. **SDK `subscribe()` blocks the calling thread** — always wrap in a daemon thread (documented in `sdk/python/README.md` "SSE push" section) unless you want your main thread blocked.
6. **`/v1/models` returns 565+ entries** — most are de-duped via canonical id, a small number have aliases. Don't be alarmed by the count; the OpenRouter scan picks up everything in their catalog.

### Recently fixed (audit trail)

- v3.5.1 — R1 cache helper extraction had a NameError silently swallowed by `try/except`. Fixed mid-development by returning the cache_decision in a tuple. **Lesson**: helper extractions that change return types must verify all downstream uses; `try/except Exception` is a debugging black hole.
- v3.5.4 — `usage_weekly_limit_tokens=20M` on the VG Anthropic-Max account caused 256% reports because Anthropic's actual Pro Max allowance is unknown / higher. Added tooltip clarification that this field is operator-imposed, not provider-actual.

### Anti-patterns to avoid

- **Don't use `try: action() except Exception: pass`** — masks real errors, makes refactors dangerous (R1 incident above)
- **Don't return raw upstream exception text** — leaks stack traces (BUG-007, BUG-008)
- **Don't soft-delete in tests** — leaves orphan state across the cluster (BUG-003, BUG-012)
- **Don't poll `/lmrh/providers` faster than 4/min per key** — rate limit will fire (use SSE subscribe instead for fresher data)
- **Don't compare ETags across cluster nodes** — they're per-node by design (BUG-011)

### Future testing investments worth making

In rough order of bang-for-buck:

1. **Add Pydantic input validation** to `/v1/messages` + `/v1/chat/completions` — closes 4 bugs at once
2. **Add `pytest-randomly`** to surface test-order dependencies (BUG-001 contributor)
3. **Spin up a localhost-only mock proxy** for integration tests so they stop polluting the production DB
4. **Add Playwright tests for v3.5.x dashboard widgets** — they're complex enough to break silently
5. **Add a "release smoke" workflow** that runs the manual probes from this QA pass automatically against a candidate release

---

## v4.3.0 QA pass notes (2026-05-18)

### Method that worked well

- **Throwaway stack on a copy of the prod DB** — `docker cp` the live
  `llm-proxy2:/app/data/llmproxy.db` to a temp dir, run the released
  `dblagbro/{llm-proxy2,whisper-bridge}:4.3.0` images against it on a private
  network with `CLUSTER_ENABLED=false`. Gives realistic testing (real
  providers, real models, real AIRI) with **zero prod pollution** — every
  test artifact (incl. the AIRI conversation from the integrated-TTS test)
  dies with the throwaway. The prod fleet got read-only checks only.

### Gotchas hit this pass

1. **Mounted-DB permissions** — the image runs as `appuser` uid 1001; a
   host-dir bind-mount shadows the image's `chown`, so SQLite fails with
   "attempt to write a readonly database" on `PRAGMA journal_mode=WAL`.
   Fix: `chmod -R 777` the mounted data dir before starting the container.
2. **`page.request` bypasses `page.route`** — Playwright's API-request
   context does not go through `page.route` rewriting and does not inherit
   the page's path-scoped session cookie. For authenticated API checks
   against a no-nginx throwaway, use `page.evaluate(fetch(...))` with
   `credentials:'include'` instead — it goes through the prefix rewrite and
   the page cookies.
3. **Headless audio** — Chromium headless has no audio device; TTS *audible*
   output is unverifiable (BUG-022). The `/api/airi/speak` call + the WAV
   payload + the `<audio>` wiring are all verified; the sound is not.
4. **App theme ≠ OS theme** — the app has its own light/dark toggle
   (`aria-label="Switch to light mode"` in the top bar); `emulate_media(
   color_scheme=...)` does nothing. Click the toggle to test light mode.
5. **Console-error counting** — a browser logs every non-2xx `fetch` as a
   console error, so negative API probes inflate the count. Count console
   errors only during a clean UI run with no deliberate 4xx probes.

### v4.3 specifics

- `/api/airi/speak` and the sidecar `/speak` both cap text at 6000 chars
  (413 over). The proxy validates empty/oversize *before* forwarding, so the
  sidecar's own empty/oversize checks are only reached on a direct call.
- `_bridge_headers()` omits the `Authorization` header when no token is set
  (httpx rejects an empty `Bearer ` value); the sidecar treats no token as
  open. Prod always sets the token.
- TTS audio + text are transient — never persisted (temp file, deleted on
  context exit), so TTS testing leaves no artifacts even on a real DB.

---

## 2026-05-19 — Verification pass on v4.3.2 (lessons)

The post-deploy verification pass exposed two defects that the original
v4.3.0 deep QA missed:

1. **BUG-025** — the grok-bridge container can appear `Up` while its
   inner service is dead. A docker-level "container is up" check is *not*
   a service health check. The deep v4.3.0 pass didn't probe the bridge's
   own `/status` endpoint; the v4.3.2 verification did, and immediately
   found a `Connection refused`. Add an explicit "sidecar inner-service
   reachability" check to the standard post-deploy script.
2. **BUG-026** — I shipped v4.3.2 without verifying the assumption that
   `bridge_url` was docker-internal. The patch was theoretically correct
   for the model I had in mind; it was a no-op for the actual model
   (shared bridge via public nginx URL). **Lesson: before shipping a
   targeted fix, read the live provider config**, don't infer the
   architecture from container topology alone. A 30-second
   `SELECT extra_config FROM providers` would have caught this.

### Strengthened practice for sidecar-related fixes

When a bug touches a sidecar-dispatched provider type (grok-web today;
v4.4 will broaden to more), verify *all* of:

- The provider row's `extra_config` (`bridge_url`, etc.) — what does the
  proxy actually call?
- DNS — what does that hostname resolve to from each node?
- The sidecar's own health/status endpoint, not just `docker ps`.
- That the proposed gate is reachable (does the patch actually fire
  under the conditions you expect?).

Treat a release ceremony as completing *only* after a targeted post-deploy
verification that exercises the changed code path on a live node and
confirms it actually fires.

---

## Mixed-version cluster-sync skew test — 2026-05-19 (BUG-037 closure)

First deliberate exercise of the BUG-037 runbook in
`docs/f3-runbooks.md`. Manufactured a controlled prod-node skew by
downgrading tmrwww02 to v4.3.5 while tmrwww01 + c1conv stayed on
v4.3.6 — then re-upgrading tmrwww02 after running the assertions.

**Why this version pair was safe to deliberately skew:** v4.3.5 →
v4.3.6 was purely additive (new `POST /api/airi/notify/_test_dispatch`
endpoint, no schema changes, no breaking protocol changes, no
existing endpoint touched). Risk = "calls to the new endpoint on the
older node return 4xx instead of 200"; that endpoint has zero
production callers, so risk = zero in practice.

**Skew window:** ~146 seconds wallclock (shorter than the runbook's
conservative 10-min ceiling; all 5 assertions cleared early so the
re-upgrade ran immediately).

**Per the runbook's 5 assertions:**

| # | Assertion | Result |
|---|---|---|
| 1 | Provider config edits propagate OLDER (4.3.5) → NEWER (4.3.6) | ✅ **PASS** — provider created on tmrwww02 observed on tmrwww01 within 30 s (sync cycle is 60 s, so well within one tick) |
| 2 | Provider config edits propagate NEWER → OLDER | ✅ **PASS** — provider created on tmrwww01 observed on tmrwww02 within 10 s |
| 3 | New endpoint that only exists on NEW_VERSION returns 4xx (not 5xx) on the older node | ✅ **PASS** — tmrwww01 (NEW) returned 401 on the new `POST /api/airi/notify/_test_dispatch` (route exists, requires auth); tmrwww02 (OLD) returned 405 (path matches a different route's prefix, POST not supported there — clean 4xx). No 5xx crash. |
| 4 | `/health` returns `status:healthy` on both nodes throughout | ✅ **PASS** — both nodes `healthy` + `10/10 providers` for the entire 146 s window |
| 5 | No error spike in `activity_log` correlated with the skew | ✅ **PASS** — 0 error/critical rows on tmrwww01 in the 3-min window; 1 row on tmrwww02 but it was a pre-existing Grok-Web probe failure (BUG-025 pattern, unrelated to the skew) |

**Cluster sync was demonstrably bidirectional and prompt under
version skew.** The 30-second OLDER→NEWER observation and 10-second
NEWER→OLDER observation both fall well inside the 60-second sync
cycle — confirming the sync protocol is forward-and-backward
compatible across v4.3.5 ↔ v4.3.6.

**Cleanup** — the two throwaway providers used to drive assertions
1+2 were deleted via API at the end of the test (200/200 on both).
The session-finish hook in `tests/conftest.py` purges any pytest-
mock tombstones; this drill used non-prefixed names so didn't
trigger it.

**Lessons applicable to future skew tests** (worth noting):
- The sync cycle is 60 s; observing a change after 30 s is "first
  cycle that included the change." A test waiting only 10 s could
  falsely report a sync failure — `wait at least 90 s` (one full
  cycle + headroom) is the correct lower bound when failure mode
  isn't obvious.
- Assertion #3's distinction between 404 and 405 is informative —
  405 means a sibling route in the same FastAPI mount accepted the
  path but rejects the method. Either is acceptable evidence of
  "clean degradation"; the bug would be a 500 (which would indicate
  the older node tried to dispatch and crashed mid-handler).

**BUG-037 closed.**

---

## 2026-05-20 — v4.4.0 release-readiness QA pass

A post-release QA pass executed immediately after the v4.4.0 ceremony to verify operational health. Findings: 3 low-severity items (`BUG-051`, `BUG-052`, `CLEANUP-001`) filed in `bug-log.md`; no critical/high defects. Documenting environmental quirks discovered here so future passes don't waste time re-discovering them.

### `/v1/models` requires `Authorization: Bearer <key>` — not anonymous

A common QA assumption is that `/v1/models` is anonymously listable (OpenAI's public endpoint shape). In this proxy it requires an API key. Smoke tests should preflight with `Authorization: Bearer llmp-...`. Both 401s observed during this pass had clean human-readable messages (`{"detail":"Missing API key"}` / `{"detail":"Invalid or disabled API key"}`).

### Cluster status endpoint is under `/cluster/status`, not `/api/...`

There is no `/api/cluster/*` route family. The single cluster status endpoint is at `/cluster/status` and requires admin session-cookie auth (not API-key auth). The HMAC endpoints are under `/api/admin/` for cross-cluster (peer-to-peer) auth.

### Bridge state is the source of truth for grok-web; check `/api/status` on the bridge container

To inspect grok-web live state, exec inside `llm-proxy2-grok-bridge` and curl `http://127.0.0.1:8443/api/status`. Reports `logged_in`, the 4 critical cookies (`cf_clearance`, `__cf_bm`, `sso`, `x-userid`), `last_refresh_status`, current conversation ID, and the 429 cooldown state. **The proxy's CB / activity log does NOT have this fidelity** — only the bridge does.

### The DB lives at `/app/data/llmproxy.db` (inside the container)

`/data/` is a vestigial empty directory from an older container image; the actual SQLite DB is at `/app/data/llmproxy.db`, and its WAL + SHM siblings sit alongside. The host-side mount is `/home/dblagbro/docker/volumes/llm-proxy2-data/...` (per compose).

### M-2 table is populated from M-3 probes even though M-4 is dormant

`provider_node_auth_state` is silently populated by the keepalive probe writer (M-3) regardless of whether any provider has `node_local_session=True`. Today's content (verified on www1) shows 3 rows for the grok-web provider — one from each node — proving cluster sync of the new key works. The routing filter (M-4) ignores those rows for all 18 providers (none flagged), so the population is informational. For Path A retry, this means activation is "flip one flag" rather than "backfill the table first."

### Unit suite count: 2260 (excluding integration), 2415 collected, 52s wall time

When collected with `pytest --collect-only -q tests/`, the count is 2415 — the difference is 155 integration tests that depend on the live deployment URL. With `--ignore=tests/integration`, the suite is 2260/2260 passing, 7 warnings, ~52s.

### Activity log error events: filter on `event_type='llm_request'` + `severity='error'`; column is `message` (not `error_message`)

The schema is `(id, event_type, severity, message, provider_id, api_key_id, event_meta, created_at)`. Common 24h baseline (verified 2026-05-20) is 80-120 errored `llm_request` events, ~80% of which come from intentional negative-test fixtures (`C1 Anthropic Claude` + `Devin-Codex-Gmail`) or expected upstream 429s on grok-web. **DO NOT flag those as defects** — see `~/.claude/projects/-home-dblagbro/memory/reference_intentional_failing_provider_fixtures.md`.

### Test-fixture providers are not garbage-collected

`pw-persist-*` (Playwright UI) and `skew-from-*` (BUG-037 drill) test runs leave provider rows behind. They're `enabled=0` so they don't route, but they inflate provider counts and push-sync payloads. CLEANUP-001 closed 2026-05-20 — all 8 test-fixture rows tombstoned; BUG-053 then surfaced and was closed in v4.4.2 to prevent future tombstone-propagation gaps.

---

## 2026-05-20 — v4.4.x fix-cycle wrap-up

Nine releases in one session (v4.4.0 → .1 → .2 → .3 → .4 → .5 → .6 → .8 → .9; v4.4.7 skipped per operator direction). All previously-open defects (BUG-051..058 + CLEANUP-001 + F-OBS-004) closed. The session's structural lessons worth keeping:

### `tools/cut-release.sh` now has a pre-cut live-verify step (v4.4.4 — L3)

Before tagging, it hits all 3 canonical `/health` URLs and aborts if any returns non-healthy. **This catches the entire "fleet silently broken at cut time" class of footgun** — exactly the v4.3.3 issue where tmrwww01 stayed on v4.3.2 because its compose used a local `llm-proxy2:latest` tag that hadn't been retagged after the docker-hub push. Escape hatch: `--skip-live-verify` for the case where the new release ITSELF is the fix for the broken state.

### `wal_checkpoint(TRUNCATE)` is now part of the daily prune sweep (v4.4.4)

Past observation: a write burst on 2026-05-13 (the RMAI 1.04B-token amplifier loop driving 27× normal proxy volume) inflated the WAL on www1 to 1.097 GB. SQLite reuses WAL pages in place across PASSIVE checkpoints — only TRUNCATE actually shrinks the file. Manual one-shot reclaimed it that day; v4.4.4 made it automatic for future bursts.

### Container resource limits are applied fleet-wide (F-OBS-004 / v4.4.x session)

Each `llm-proxy2` runs with `deploy.resources.limits: {cpus: 4, memory: 4G}`; the www1 grok-bridge gets `{cpus: 2, memory: 2G}`. A memory leak in either container will now OOM-kill the offending container rather than starve the host's other workloads. The historical ARCH-A pool leak (saturating at 13-20h post-deploy) is the canonical "why this matters" case.

### Activity log orphan FK refs are now cleaned in the daily sweep (BUG-055 / v4.4.3)

Provider / api_key tombstones get hard-deleted after `provider_tombstone_retention_days` (default 7), but the activity_log rows that referenced them survive. SQLite has no FK enforcement. Pre-fix audit found **438 orphan provider_ids + 7,937 orphan api_key_ids** on www1; one-shot cleanup deleted 21,230 dangling refs across the 3-node fleet. v4.4.3's `_prune_activity_log_orphans()` runs daily after the tombstone-prune step.

### Cluster-sync tombstone propagation is now branch-on-`not local_deleted` (BUG-053 / v4.4.2)

The old gate `peer_deleted_at >= local_updated` silently dropped tombstones when background activity on the receiver bumped `local.updated_at` past the originator's `deleted_at` timestamp. New gate is "peer has tombstone, local doesn't → accept" — tombstones are terminal in this app, so the "fresher updated_at on the receiver" concern is moot. The original symptom (`skew-from-new-41a9d6` stranded on www1 for 18 hours) was already manually reconciled in v4.4.1's session; the v4.4.2 fix is preventive.

### Streaming-protocol completeness: BUG-056 (Anthropic) + BUG-057 (OpenAI)

Two surface-level streaming-translator gaps that surfaced together in the L1 `--run-real` matrix run:

- **BUG-056 (v4.4.5)**: Gemini providers sometimes emit a stream with no `delta.content` chunks at all (Gemini buffers a short response into the terminator chunk). The proxy used to skip `content_block_start` / `content_block_stop` in that case — leaving the stream structurally invalid per the Anthropic streaming protocol. Fix: emit a synthetic empty text block when neither text nor tool content was seen.
- **BUG-057 (v4.4.6)**: modern OpenAI streaming (litellm 1.83.x default) emits a usage chunk AFTER the finish_reason chunk. The proxy used to pass through verbatim, so the LAST emitted chunk had `finish_reason=null` — breaking SDK clients' end-of-stream detection. Fix: buffer-and-patch — track most recent finish_reason; patch the last chunk in place before serializing.

Both fixes were verified live against the deployed image via the `--run-real` matrix tests.

### `--run-real` matrix tests are destructive-to-monitoring while running

The `_all_providers_with_cb_cycling` helper deliberately force-opens every provider's CB to test routing fall-through. Mid-test, the UI on the running node shows N providers in `open` state simultaneously. Cleanup restores them at `StopIteration` of the iterator, but operators watching the UI mid-run will see "every provider tripped" for a few minutes. **Coordinate before running `--run-real`** — warn the operator that the UI will look broken for ~5 min, or run during quiet hours.
