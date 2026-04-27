# QA Notes — llm-proxy-v2

Operational quirks, environment assumptions, flaky behavior, and risk
notes accumulated during regression sweeps. Update freely; this file is
deliberately less structured than `bug-log.md` or `test-plan.md`.

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
