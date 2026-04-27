# Bug Log — llm-proxy-v2

Persistent log of defects, regressions, and quality gaps discovered during
QA/regression sweeps. Add new findings at the top with the most recent
sweep date as the section header.

Severity ladder: **critical** > **high** > **medium** > **low** > **enhancement**

Status flow: **open** → **in-progress** → **fixed** → **verified-fixed** → **wont-fix**

---

## 2026-04-24 — post-v2.7.5 deep regression sweep

Driver: comprehensive post-OAuth-rollout validation. Production cluster
on v2.7.5 across 3 nodes. Devin-VG provider configured. 633 unit tests
passing; 7 integration tests failing on first run (analyzed below).

### BUG-001 [CRITICAL] Streaming requests mask auth/upstream errors with HTTP 200

- **Area**: `/v1/messages` streaming path, `app/api/_messages_streaming.py`
- **Repro**:
  1. Configure or have an enabled anthropic provider with a stale/invalid `x-api-key`
  2. POST `/v1/messages` with `stream: true` so it routes to that provider
- **Expected**: HTTP 5xx OR automatic failover to the next-priority anthropic-capable provider
- **Actual**: HTTP **200**, SSE body is exactly:
    ```
    data: {"type": "error", "error": {"message": "litellm.AuthenticationError ... invalid x-api-key ..."}}
    data: {"type":"message_stop"}
    data: [DONE]
    ```
- **Impact**: Clients that only check status_code see "success", consume an empty stream, and surface a confusing UX. Auth misconfiguration becomes invisible until users complain.
- **Likely cause**: streaming path catches exceptions from the upstream call but emits an SSE error event and a synthetic `message_stop` instead of (a) returning a non-200 status before the SSE starts, or (b) entering the failover ladder.
- **Suggested fix**:
    - For pre-stream auth errors (401/403), return an HTTP error status BEFORE the body starts streaming.
    - Inside the SSE stream, on a fatal upstream error, attempt failover to the next-priority capable provider. Only emit an SSE error event if all candidates fail.
    - Mark provider failures as failures in the circuit breaker (currently uncertain — see BUG-003).
- **Status**: open

### BUG-002 [HIGH] Persistent auth_error not auto-disabling broken providers

- **Area**: provider lifecycle / circuit breaker
- **Repro**: `POST /api/providers/{id}/test` against the two broken anthropic providers (`Anthropic Claude Code #3`, `C1 Anthropic Claude`) returns `success=false` with `litellm.AuthenticationError ... invalid x-api-key`. The providers remain `enabled=true, priority=1` and continue receiving routed traffic.
- **Expected**: After N consecutive auth failures, provider should auto-disable (or stay circuit-broken indefinitely until admin intervenes), since auth errors are NOT transient — retrying every N seconds will not fix anything.
- **Actual**: Standard circuit breaker hold-down (~120s) + reset, then they're tried again on the next request, fail again. Permanent waste of latency.
- **Suggested fix**:
    - In `circuit_breaker.is_billing_error()`-style classifier, add an `is_auth_error()` classifier that maps 401/403 + body-text matches to a permanent-breaker state.
    - Surface it in the UI with a red "Auth failure — re-key required" badge so admins can fix or disable it.
- **Status**: open

### BUG-003 [HIGH] OAuth access_token can be revoked server-side without local visibility

- **Area**: `app/providers/claude_oauth.py`, `_messages_streaming._complete_claude_oauth`
- **Repro**:
  1. Authorize a `claude-oauth` provider; `oauth_expires_at` = now + 8h.
  2. ~3h later, request `/v1/messages` against it → returns `401 "Invalid authentication credentials"`.
  3. `oauth_expires_at` still indicates the token is valid for ~5h more.
- **Expected**: On a 401, the proxy auto-refreshes via `refresh_and_persist()` and retries the request once.
- **Actual**: 401 propagates straight to the caller. No refresh, no retry, no failover. `oauth_expires_at` is treated as authoritative when it isn't.
- **Likely cause**: `refresh_and_persist` exists (v2.7.5) but is not wired into the request path; messages dispatch never observes the 401.
- **Suggested fix**: In `_complete_claude_oauth` and `_stream_claude_oauth`, on 401 from upstream:
  1. Call `refresh_and_persist(provider, db)`
  2. Rebuild headers with the fresh token
  3. Retry once
  4. If still 401 OR refresh fails with `invalid_grant`, return 401 to caller AND mark provider with a "needs re-auth" status surfaced in UI
- **Status**: open

### BUG-004 [MEDIUM] Brittle hardcoded version assertion in integration tests

- **Area**: `tests/integration/test_auth.py::test_health_is_public`
- **Repro**: `python3 -m pytest tests/integration/test_auth.py::TestUnauthorized::test_health_is_public`
- **Expected**: Test passes against any deployed version
- **Actual**: `assert d["version"] == "2.0.0"` — fails for every version > 2.0.0 (currently 2.7.5)
- **Fix**:
    ```python
    assert re.match(r"^\d+\.\d+\.\d+$", d["version"])
    ```
- **Status**: open

### BUG-005 [HIGH] Streaming integration tests cannot distinguish "happy path" from "upstream error"

- **Area**: `tests/integration/test_routing_mock.py::TestAnthropicStream`, `TestOpenAIStream`
- **Repro**: Run any stream test; the fixture sets up a mock provider, but the stream lands on a broken real provider that emits `{"type":"error",...}`. Tests `KeyError` on parsed events because they assume `e["type"]` is a known content event.
- **Expected**: The fixture either guarantees a working mock-only routing (no real providers in the candidate set), or the test asserts on `r.status_code != 200` first.
- **Actual**: 7 stream-related integration tests fail because of upstream provider auth errors leaking into the stream. The mock fixture's `cluster/circuit-breaker/{id}/open` calls evidently aren't enough to keep traffic off the broken anthropic providers.
- **Suggested fix**:
    - Add explicit assertion in `collect_sse` consumers that no event has `type=="error"` (fail-loud).
    - Augment `only_mock_routing` fixture: in addition to circuit-breakering, set `enabled=False` on every non-mock provider for the test scope, then restore.
- **Status**: open

### BUG-006 [MEDIUM] `_inject_claude_code_system` may break prompt caching when caller's first system block has cache_control

- **Area**: `app/api/_messages_streaming.py::_inject_claude_code_system`
- **Repro**: Caller sends `system: [{"type":"text","text":"...","cache_control":{"type":"ephemeral"}}]`.
  After injection: `system: [{"type":"text","text":"You are Claude Code..."}, {"type":"text","text":"...","cache_control":{"type":"ephemeral"}}]`.
  The caller's cached prefix changes between requests because the marker block is non-cacheable (no `cache_control`) and prepended.
- **Expected**: Caller's cache_control prefix continues to hit the cache after the proxy adds the marker.
- **Actual**: For a NEW caller (first time hitting the proxy), the prefix is now `[marker_block, user_block]` — but Anthropic's caching is keyed by content including the marker. So caching still works for repeated proxy calls, but anyone migrating from direct Anthropic API → proxy loses cache state on day 1 (different prefix).
- **Severity downgrade rationale**: caching still works for repeat traffic *through the proxy*; this is migration friction not a runtime defect. Still worth a doc note + a `cache_control` on the marker block to keep the prefix stable.
- **Suggested fix**: Add `"cache_control": {"type": "ephemeral"}` to the injected marker block so it joins the cached prefix.
- **Status**: open

### BUG-007 [LOW] OAuth refresh-token rotation pitfall easy to hit

- **Area**: `app/providers/claude_oauth_flow.py`
- **Repro**: Any caller that uses `refresh_access_token()` directly (not `refresh_and_persist()`) will consume the refresh token from the DB without writing the rotated one back. Next refresh fails with `invalid_grant` until admin re-runs the OAuth flow.
- **Mitigation in place (v2.7.5)**: `refresh_and_persist()` helper exists; live test docstring warns about the trap.
- **Open risk**: nothing prevents direct callers from grabbing `refresh_access_token` (still publicly exported). A static analysis rule or a deprecation warning would help.
- **Suggested fix**: Mark `refresh_access_token` as `_internal_refresh_access_token` (single underscore + comment) so the discoverable name is the safe one. Or have it raise unless called from `refresh_and_persist`.
- **Status**: open

### BUG-008 [HIGH] No production wiring for `refresh_and_persist` — token expiry/revocation requires admin re-auth

- **Area**: `app/api/_messages_streaming.py`, scanner.py, scheduled jobs
- **Repro**: see BUG-003 — there's no place in the request lifecycle that calls `refresh_and_persist`. The helper exists but is unused.
- **Expected paths that should call it**:
    1. `_complete_claude_oauth` and `_stream_claude_oauth`: catch 401, refresh-and-retry once.
    2. A periodic background task: every ~60min, refresh tokens whose `oauth_expires_at - now < 600s`.
    3. `scan_provider_models` and `_test_claude_oauth`: same 401 retry.
- **Status**: open

### BUG-009 [MEDIUM] Docs claim default credentials `admin/admin` but real production password differs

- **Area**: `README.md`
- **Repro**: README says "Default login: admin / admin — change immediately after first boot." Production cluster uses `Super*120120` (per `tests/conftest.py`).
- **Risk**: A new admin reading the README will fail to log in and assume the system is broken; or worse, if they SQL-poke the admin row to "fix" it, they may overwrite a working password in production.
- **Suggested fix**: README should clarify "On first boot only. Change in production via the Users page; the test fixtures use `Super*120120` for the existing admin."
- **Status**: open

### BUG-010 [MEDIUM] Two anthropic providers with identical priority=1 — non-deterministic routing

- **Area**: provider table / routing tiebreaker
- **Repro**: `Anthropic Claude Code #3` (anthropic, broken) and `Devin-VG` (claude-oauth, working) both have `priority=1`. LMRH ranking + CB status determines selection but the order is implementation-defined when scores tie.
- **Expected**: Either explicit tiebreaker (creation time / id ordering) or a UI warning when two enabled providers share a priority.
- **Actual**: Tiebreaker behavior is implicit (likely DB row order). Two consecutive identical requests may land on different providers.
- **Suggested fix**: When two enabled providers share `priority`, surface a yellow warning badge in the Providers UI and document the tiebreaker rule (probably `created_at` ascending).
- **Status**: open

### BUG-011 [resolved] Stale references to deleted `oauth_capture/terminal.py` or sidecar may exist

- **Area**: post-v2.7.0 cleanup
- **Repro**: `grep -rn "terminal\.py\|sidecar" app/ frontend/src/`
- **Result**: only residual *comments* found; no live code or imports. Closed as **verified-clean**.
- **Status**: verified-clean

### BUG-012 [ENHANCEMENT] Burn-test refresh path needs a "tear-down" mode

- **Area**: `scripts/test_claude_oauth_live.py`
- **Issue**: Each invocation rotates the refresh token. If anything in the rotation chain breaks, the next run fails until admin re-auths.
- **Suggested fix**: Add a `--skip-destructive` flag to `t_refresh_and_persist` so the suite can be re-run without consuming the refresh token.
- **Status**: open

### BUG-014 [MEDIUM] Activity log severity filter does literal-string match on comma-separated values

- **Area**: `/api/monitoring/activity` query handler
- **Repro**: `GET /api/monitoring/activity?severity=warning,error`
- **Expected**: returns events whose severity is `warning` OR `error`
- **Actual**: returns 0 events (matches literal column value `"warning,error"` which never exists)
- **Suggested fix**: `query.where(ActivityLog.severity.in_(severity.split(",")))` instead of `==`.
- **Status**: open

### BUG-015 [LOW] index.html served without Cache-Control

- **Area**: FastAPI SPA fallback / nginx
- **Repro**: `curl -I https://www.voipguru.org/llm-proxy2/`
- **Expected**: `Cache-Control: no-cache` (or `max-age=0, must-revalidate`) on the SPA shell so users always get the latest asset hashes after a deploy.
- **Actual**: no Cache-Control header at all. Browsers may cache index.html briefly and load stale asset hashes.
- **Suggested fix**: add `Cache-Control: no-cache` to the SPA shell response in `app/main.py` catch-all handler.
- **Status**: open

### BUG-016 [LOW] Playwright provider Test-button assertion uses stale copy

- **Area**: `tests/integration/test_playwright_ui.py::TestProviderActions::test_provider_test_button_shows_result`
- **Repro**: assertion is `span:text-matches('^OK$|^Error$')` but actual UI text is `Test OK` / `Test failed`.
- **Suggested fix**: either change the regex to `^Test (OK|failed)$` or change the badge text to a single-word `OK`/`Error`.
- **Status**: open

### BUG-017 [HIGH] No DB index on `api_keys.key_hash` — every authenticated request does a full table scan

- **Area**: schema (`app/models/db.py`)
- **Repro**: `SELECT name FROM sqlite_master WHERE type='index'` returns one row only (`ix_oauth_capture_log_capture_session`). The `api_keys.key_hash` column is the predicate on every authenticated request and has no index.
- **Expected**: `CREATE INDEX ix_api_keys_key_hash ON api_keys(key_hash)` or use `unique=True, index=True` on the column model.
- **Actual**: full scan; OK at 115 rows, painful at 10K+.
- **Severity**: HIGH not because of current pain but because it grows linearly with key count and isn't backfilled by any migration.
- **Suggested fix**: add `index=True` on `key_hash`, `provider_id` (provider_metrics, activity_log), `bucket_ts` (provider_metrics), `created_at` (activity_log), `token` (sessions).
- **Status**: open

### BUG-018 [MEDIUM] No request-level failover for non-streaming `/v1/messages` when first provider returns 401/auth-error

- **Area**: `app/api/messages.py` and `app/routing/fallback.py`
- **Repro**: send a request with the api-key configured at `priority=1` returning 401 from upstream. Proxy returns 401 to client without attempting next-priority provider.
- **Expected**: retry against next-priority capable provider, ESPECIALLY for non-billing auth errors (the request is well-formed; the provider is broken).
- **Actual**: bubbles the 401/502 out to the client.
- **Note**: this affects all provider types AND is intentionally short-circuited for `claude-oauth` (per comment in messages.py: "Claude Pro Max already runs through Claude Code's server-side routing, so we just forward..."). For OAuth this is fine when the token is good; when the token is revoked it produces user-facing 401s.
- **Suggested fix**: gated behind `settings.fallback_enabled`, retry on 401/403 against the next ranked provider once. For claude-oauth specifically, attempt `refresh_and_persist` first before failing over.
- **Status**: open

### BUG-019 [LOW] Provider creation endpoint accepts empty `api_key` for provider types that require auth

- **Area**: `POST /api/providers`, `app/api/providers.py`
- **Repro**: POST a `google` provider with `api_key=""` succeeds. The provider is enabled but every request to it 502s with `Missing Gemini API key`.
- **Expected**: validate that `provider_type in {anthropic, openai, google, vertex, grok}` requires `api_key` (or `oauth_credentials_blob`/`oauth flow` for `claude-oauth`).
- **Actual**: silently accepts empty string. Same for editing.
- **Suggested fix**: pre-flight check in `create_provider` and `update_provider`. UI may need a counterpart so admins see a clear error.
- **Status**: open

### BUG-013 [ENHANCEMENT] No version field validation across OpenAPI/health/cluster

- **Area**: release process
- **Issue**: Version strings live in `app/main.py` (5 occurrences), `app/api/cluster.py`, plus README sample, plus tests. Each release we manually `sed` them. One day someone forgets one.
- **Suggested fix**: Single source of truth — `app/__version__.py` reading `pyproject.toml` or a generated file. README sample and tests use a regex.
- **Status**: open

---

## Remediation Plan

### Tier 1 — release blockers (fix before next user-visible release)

1. **BUG-001** Streaming masks errors as 200 → 5xx-on-pre-stream-error + failover or fail-loud
2. **BUG-003** OAuth 401 not auto-refreshing → wire `refresh_and_persist` into 401-retry in both messages handlers
3. **BUG-008** `refresh_and_persist` not used in production → same wire-up as above + a periodic background refresh job for tokens approaching expiry
4. **BUG-018** No failover on auth errors → respect `settings.fallback_enabled` for 401/403 too

### Tier 2 — operator pain / data-quality

5. **BUG-002** Auth errors not classified as permanent → add `is_auth_error()` classifier; auto-disable provider after N consecutive auth failures and surface in UI
6. **BUG-017** Missing DB indexes → add migration for `api_keys.key_hash`, `activity_log.created_at`, `activity_log.provider_id`, `provider_metrics.(provider_id, bucket_ts)`, `sessions.token`
7. **BUG-014** Activity severity comma-list → `IN (...)` query
8. **BUG-019** Empty `api_key` accepted on create → preflight validation
9. **BUG-010** Two providers same priority → UI warning + documented tiebreaker

### Tier 3 — quality / hardening

10. **BUG-006** `_inject_claude_code_system` marker should carry `cache_control: ephemeral`
11. **BUG-007** Mark `refresh_access_token` as `_internal_*` to discourage direct use
12. **BUG-013** Single-source-of-truth version → `app/__version__.py`
13. **BUG-004** Test version assertion → regex
14. **BUG-005** `only_mock_routing` fixture → also disable non-mock providers, and `collect_sse` should fail-loud on `event.type=='error'`
15. **BUG-009** README admin/admin doc fix
16. **BUG-015** index.html `Cache-Control: no-cache`
17. **BUG-016** Playwright Test-button assertion → match real copy
18. **BUG-012** Burn test `--skip-destructive` flag

### Quick wins (≤30 min each)

- BUG-004, BUG-009, BUG-014, BUG-015, BUG-016 — all small textual / one-liner fixes
- BUG-006 — single-line edit
- BUG-019 — ~5 lines of validation

### Architectural fixes (need design pass)

- BUG-001 + BUG-018 — proper SSE error semantics + fallback contract for streaming
- BUG-002 + BUG-008 — provider auth-error lifecycle (classifier → CB → UI badge → auto-disable)
- BUG-017 — schema migration for indexes (and probably an alembic migration framework if not already in use)

### Recommended retest after each tier

| After Tier | Retest |
|---|---|
| 1 | Live OAuth burn test + a "deliberate broken-key" integration test (provision provider with a known-bad key, confirm: failover happens once, second 401 returns 5xx, provider transitions to disabled state) |
| 2 | DB index sanity (`PRAGMA index_list(...)`), repeat the live API key auth latency, confirm activity_log severity filter works |
| 3 | Full integration suite + Playwright; confirm version-regex test passes against any version |

---

## Last verified passing surfaces (for context)

- **Unit suite**: 633/633 passing (`python3 -m pytest tests/unit/`)
- **Cluster sync heartbeats**: 3/3 nodes healthy in last cycle
- **OpenAPI schema**: 53 paths, all have operationId
- **Provider CRUD**: roundtrip works, 404 after delete
- **Settings PUT round-trip**: persists correctly
- **RBAC**: non-admin → 403 on `/api/providers`, `/api/settings`, `/api/users`
- **Auth gate**: missing/bogus key → 401; bad password → 401
- **Rate limit**: 5/6 of 6-rapid-hits at RPM=3 → 429
- **Activity SSE stream**: emits live events
- **`refresh_and_persist` (mocked)**: 3 unit tests pass
- **`/v1/models`**: 12 models served
- **OAuth `/authorize` endpoint**: 401 unauth, 200 auth with valid PKCE URL

---

## Confirmed-fixed (kept for context)

- v2.7.1 → v2.7.2: wrong authorize URL + client_id → user-facing "error logging you in" — **fixed**
- v2.7.2 → v2.7.3: missing CC system marker → masked rate_limit_error — **fixed**
- v2.7.3 → v2.7.4: scan_models returned `[]` for claude-oauth — **fixed**
- v2.7.4 → v2.7.5: Haiku 400 with 1M-context beta + refresh-token rotation drop — **fixed**
