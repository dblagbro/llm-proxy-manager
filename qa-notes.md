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
- Admin password used by the integration suite is hardcoded as `Super*120120` in `tests/conftest.py`. README still says `admin/admin` (BUG-009).
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
