# Test Plan — llm-proxy-v2

## Validation Scope

| Sweep type | Triggers | Surfaces |
|---|---|---|
| **smoke** | Every deploy | health, /v1/messages happy path on Devin-VG, login |
| **standard regression** | Pre-release | unit suite + non-Playwright integration + 1 Playwright pass + basic API probes |
| **deep regression** (this sweep) | Major refactors, OAuth changes, before-release hardening | every surface below + adversarial / negative paths |

## Surface Inventory & Validation Method

| # | Surface | Method | Status today |
|---|---|---|---|
| 1 | `/health` (public) | curl, version regex | ✅ live |
| 2 | `/openapi.json` | curl + jq, operationId/each path | ✅ valid |
| 3 | Admin login | curl + cookie jar, bad-pw rejection | ✅ working |
| 4 | Session RBAC | non-admin user → 403 on admin endpoints | ✅ enforced |
| 5 | API key auth | bogus/missing key → 401 | ✅ enforced |
| 6 | API key rate limit | 6 rapid hits at RPM=3 → 429 majority | ✅ working |
| 7 | API key spending cap | configure $0.0001 cap, expect 402 | ⚠ inconclusive (broken upstreams masked it) |
| 8 | `/v1/messages` non-streaming, happy path | live API key + working provider | ⚠ all real providers broken (BUG-002, BUG-003) |
| 9 | `/v1/messages` streaming SSE | curl --no-buffer, parse events | 🛑 BUG-001: errors masked as 200+SSE-error |
| 10 | `/v1/chat/completions` | curl, validate `choices[0].message` | 🛑 502 due to broken upstreams |
| 11 | `/v1/models` | curl, count >= configured | ✅ 12 models |
| 12 | Provider CRUD | POST → GET → DELETE → 404 | ✅ working |
| 13 | Provider Test button | POST `/api/providers/{id}/test` | ✅ returns; UI badge text mismatch (BUG-016) |
| 14 | Provider Scan Models | POST `/api/providers/{id}/scan-models` | ✅ for OAuth (when token valid) |
| 15 | Settings GET/PUT round-trip | flip cot_enabled, reread, restore | ✅ working |
| 16 | Settings unknown-key rejection | PUT junk → 400 | ✅ working |
| 17 | Activity log list | `/api/monitoring/activity` | ✅ paginating |
| 18 | Activity log filter | `?severity=warning,error` | 🛑 BUG-014: comma-list does literal-match |
| 19 | Activity SSE stream | `/api/monitoring/activity/stream` | ✅ streaming |
| 20 | Cluster status | `/cluster/status` | ✅ 3/3 healthy |
| 21 | Frontend SPA shell | curl `/` + asset | ✅ valid; missing Cache-Control on `index.html` (BUG-015) |
| 22 | SPA deep-link | curl `/providers` returns HTML fallback | ✅ working |
| 23 | OAuth `/authorize` endpoint | unauth→401, auth→200 with PKCE URL | ✅ correct |
| 24 | OAuth `/exchange` endpoint | unit-tested + live one-shot | ✅ when state matches |
| 25 | `claude-oauth` `_complete` handler | direct call (burn test) | ⚠ now 401 — token revoked (BUG-003) |
| 26 | `claude-oauth` `_stream` handler | burn test bytes + event-order check | ⚠ now 401 — token revoked |
| 27 | `_inject_claude_code_system` | unit + 4-shape covered | ⚠ no `cache_control` on marker (BUG-006) |
| 28 | `refresh_and_persist` | unit (mocked httpx) | ⚠ not wired into prod paths (BUG-008) |
| 29 | LMRH `;require` honored | `region=mars;require` → 503 | ⚠ inconclusive (rate limit on real probe; covered by unit tests) |
| 30 | Circuit breaker open/reset | `/cluster/circuit-breaker/{id}/{open\|reset}` | ✅ via Playwright |
| 31 | DB schema parity | inspect ALTERs vs models | ✅ all columns present |
| 32 | DB index hygiene | `SELECT name FROM sqlite_master WHERE type='index'` | 🛑 BUG-013: only 1 non-PK index |
| 33 | Webhook delivery | unit + signature deterministic | ✅ unit-tested |
| 34 | Logs / observability | activity_log + container stderr | ⚠ noisy with auth errors due to broken keys |
| 35 | Background jobs (heartbeat) | cluster shows fresh `last_heartbeat` | ✅ 3/3 within last cycle |
| 36 | Webhook on completion | unit-tested via `_FakeClient` | ✅ |
| 37 | Audit log export (S3) | unit-tested only | ⚠ no live integration check (out of scope for this sweep) |
| 38 | Tool emulation | integration tests | ✅ all 3 PASS now |

## Pytest baseline

| Suite | Command | Result |
|---|---|---|
| Unit | `python3 -m pytest tests/unit/` | **633 / 633 passing** |
| Integration (no UI) | `python3 -m pytest tests/integration/ --ignore=tests/integration/test_playwright_ui.py` | 50 passed, 13 skipped, **6 failed** (BUG-004, BUG-005, BUG-018) |
| Integration UI | `python3 -m pytest tests/integration/test_playwright_ui.py` | 46 passed, **1 failed** (BUG-016) |
| Live OAuth burn | `scripts/test_claude_oauth_live.py` | 16 / 17 PASS (was 16/17 last run; one expected red) — now will fail more due to BUG-003 |

## Coverage Gaps (will not get fixed in this sweep)

1. No live provider test for OpenAI / Google / Vertex / Grok / Ollama / Compatible — all the env keys are missing or stubbed.
2. No integration test for `refresh_and_persist` 401-retry path because the path isn't wired (BUG-008).
3. No load test or concurrency soak — burn test does 5x parallel but nothing larger.
4. No Playwright test exercises the **Generate Auth URL** browser flow (would need a real OAuth handshake).
5. No test for cluster-sync write conflict resolution.
6. No fuzz test for `extract_code_from_callback` parser variants.
7. No semantic-cache hit/miss live test — we only verified Anthropic prompt-cache (BUG-006 caveat).

## High-Risk Areas (recommend continuous re-test)

1. claude-oauth dispatch (recent code, narrow live coverage, server-side token revocation possible)
2. SSE path under upstream errors (BUG-001 surfaced this)
3. Auth-error provider classification (BUG-002 + BUG-003)
4. Refresh-token rotation lifecycle (BUG-007 + BUG-008)
5. Hardcoded version assertions in tests (BUG-004 — class of bug)

## Re-test scope after each remediation tier

- After **BUG-001** fix: rerun `tests/integration/test_routing_mock.py::TestAnthropicStream` + manual streaming probe with a deliberately broken provider in front of a working one. Expect failover or HTTP 5xx; never SSE-error masking 200.
- After **BUG-002 / BUG-003** fix: rerun the whole `test_claude_oauth_live.py` and a targeted "broken-provider" integration test (provision a provider with a known-bad key, confirm it auto-disables after N failures and surfaces a UI badge).
- After **BUG-008** wired: token-revocation drill — manually invalidate Devin-VG by calling the refresh endpoint twice externally, then issue traffic and confirm proxy auto-refreshes once and recovers.
- After **BUG-013** indexes added: run a 1k-row activity_log query benchmark (target: <50ms p95).
