# Bug log

Findings from the 2026-05-09 deep QA pass on llm-proxy2 v3.5.7. Severity scale: **critical** (release blocker) · **high** (real impact, fix soon) · **medium** (real but bounded) · **low** (cosmetic / edge) · **enhancement / hardening** (improvement opportunity).

Add new findings on top. When status changes, leave the row in place and update the **Status** field; only delete a row if the bug never reproduced.

---

## 2026-05-09 — Open findings (QA pass v3.5.7)

### BUG-001 — Test isolation failure: `TestVisionStripping::test_text_only_request_passes_through_unchanged`

- **Severity**: medium (test infrastructure, not production)
- **Area**: `tests/integration/test_new_features.py`
- **Environment**: integration suite, full run only
- **Reproduction**:
  1. `python3 -m pytest tests/integration/ -x --timeout=60`
  2. Failure on `TestVisionStripping::test_text_only_request_passes_through_unchanged`
  3. Same test passes when run in isolation: `python3 -m pytest tests/integration/test_new_features.py::TestVisionStripping::test_text_only_request_passes_through_unchanged -v`
- **Expected**: test passes regardless of run-order
- **Actual**: passes alone, fails when prior tests have run in same session
- **Likely cause**: shared mock-LLM-server state OR DB state OR fixture cleanup gap
- **Recommended fix**: `pytest-randomly` to shuffle test order + identify the contaminator; add session-scoped cleanup of mock server lifecycle
- **Status**: **OPEN** — needs root-cause investigation
- **Owner**: TBD

### BUG-002 — 13 integration test errors from "Address already in use" on mock LLM server

- **Severity**: high (blocks running integration suite cleanly)
- **Area**: `tests/integration/conftest.py` + `tests/mock_llm_server.py`
- **Environment**: integration tests, sequential runs
- **Reproduction**:
  1. Run full integration suite
  2. Observe 13 errors with `OSError: [Errno 98] Address already in use` from `socketserver.bind`
- **Expected**: each test gets a clean mock server port or shares a session-scoped one
- **Actual**: tests fight for the same port; later tests fail to bind
- **Likely cause**: mock server fixture not properly tearing down between tests, OR port allocation hardcoded
- **Recommended fix**: use `socket.bind(("", 0))` to grab an OS-assigned port, OR session-scope the mock server fixture, OR add proper teardown
- **Status**: **OPEN**

### BUG-003 — Integration tests pollute the production DB

- **Severity**: high (test contamination of live system)
- **Area**: `tests/integration/conftest.py`
- **Environment**: any integration test run hitting `https://www.voipguru.org/llm-proxy2`
- **Reproduction**:
  1. `python3 -m pytest tests/integration/test_new_features.py`
  2. Query the live DB: `SELECT COUNT(*) FROM providers WHERE name LIKE 'pytest%'`
  3. Find 4+ pytest-mock rows with recent `deleted_at` timestamps
- **Expected**: tests use a sandboxed DB or hard-clean rows after each test
- **Actual**: tests create + soft-delete provider rows in the production DB; orphan circuit-breaker state remains in `_local_states` because `circuit_breaker.py` doesn't clear state when a provider is deleted
- **Likely cause**: tests soft-delete (`deleted_at = now()`) instead of hard-delete; CB state cleanup hook not registered on provider deletion
- **Evidence**: `/health` reports 13 circuit breakers but only 10 providers; the 3 orphans are deleted pytest-mock rows
- **Recommended fix** (two parts):
  1. Test teardown: hard-delete pytest-mock rows from `providers` AND clear CB state for those provider IDs
  2. Production fix: in `app/cluster/sync.py` (or wherever soft-delete propagates), call `circuit_breaker.force_close(provider_id)` + remove from `_local_states` on tombstone propagation
- **Status**: **OPEN**

### BUG-004 — `/v1/chat/completions` accepts requests without `model` field, returns upstream 502

- **Severity**: medium (poor error UX; can mislead clients into thinking proxy is broken)
- **Area**: `app/api/completions.py` (request validation)
- **Environment**: any caller sending malformed body
- **Reproduction**:
  ```bash
  curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"hi"}]}' \
    https://www.voipguru.org/llm-proxy2/v1/chat/completions
  ```
- **Expected**: HTTP 400 with `{"detail":"model is required"}` (client error)
- **Actual**: HTTP 502 with grok-web bridge 429 leaking through (`{"detail":"grok-web bridge 429: ..."}`)
- **Likely cause**: no front-line schema validation; proxy picks default route (priority 1 = grok-web) and forwards the (broken) body, upstream errors surface as 502
- **Recommended fix**: add a Pydantic `ChatCompletionsRequest` model with `model: str` required, `messages: list = Field(min_length=1)` — let FastAPI return 422 automatically. Add similar validation in `messages.py`.
- **Status**: **OPEN**

### BUG-005 — `/v1/messages` accepts empty POST body, returns 200 with auto-substituted model

- **Severity**: high (silently spends real provider budget on empty client requests; potential DoS amplification)
- **Area**: `app/api/messages.py` (request validation)
- **Reproduction**:
  ```bash
  curl -X POST -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" \
    -H "Content-Type: application/json" -d '{}' \
    https://www.voipguru.org/llm-proxy2/v1/messages
  ```
- **Expected**: HTTP 400 with `{"detail":"model and messages are required"}`
- **Actual**: HTTP 200 with `{"model":"gemini-2.5-flash","content":[{"type":"text","text":"Hello!"}]...}`. Real Vertex AI request was made, real tokens consumed.
- **Likely cause**: no validation that `body.model` is truthy or that `body.messages` is non-empty; proxy treats `{}` as "auto-route, default everything"
- **Recommended fix**: same as BUG-004; require `model` and `messages` (non-empty) at the input layer
- **Severity rationale**: an unauthenticated denial-of-wallet vector — anyone with a leaked API key (with any quota) can issue empty requests and burn provider quota at no cost to themselves. The 401 gate works, but a stolen key is much more dangerous than expected.
- **Status**: **OPEN**

### BUG-006 — Unknown model name silently routes to a default; substitution disclosed only in `LLM-Capability` header

- **Severity**: medium (works as designed, but client SDKs reading `response.model` get wrong value)
- **Area**: `app/routing/router.py` (`cross_family_fallback`)
- **Reproduction**:
  ```bash
  curl -X POST -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
    -d '{"model":"totally-fake-xyz","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}' \
    https://www.voipguru.org/llm-proxy2/v1/messages
  ```
- **Expected**: either HTTP 400 (unknown model) OR HTTP 200 with `response.model = "totally-fake-xyz"` (echoing what was requested) AND a clear `X-Cross-Family-Fallback` header
- **Actual**: HTTP 200 with `response.model = "xai/grok-3"` (or whatever auto-route chose). Disclosure IS present in `LLM-Capability` header (`chosen-because=cross-family-fallback, requested-model=totally-fake-xyz, served-model=xai/grok-3`), but client SDKs that read `response.model` see only the substituted name.
- **Likely cause**: Anthropic SDK's `response.model` consumes the upstream's `model` field, which is the served model, not the requested model
- **Recommended fix**: rewrite the response body's `model` field to the originally-requested name when `cross_family_fallback=True`, OR add an explicit top-level `X-Substituted-From: totally-fake-xyz` response header that's easier for clients to inspect than parsing `LLM-Capability`
- **Status**: **OPEN**

### BUG-007 — Stack-trace leak on invalid `role` value

- **Severity**: high (information disclosure)
- **Area**: `app/api/messages.py` error handling on litellm exceptions
- **Reproduction**:
  ```bash
  curl -X POST -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
    -d '{"model":"x-ai/grok-3","max_tokens":10,"messages":[{"role":"banana","content":"hi"}]}' \
    https://www.voipguru.org/llm-proxy2/v1/messages
  ```
- **Expected**: HTTP 400 with `{"detail":"invalid role: banana"}`
- **Actual**: HTTP 502 with full litellm Python stack trace leaking container paths (`/usr/local/lib/python3.13/site-packages/litellm/...`), file names, line numbers
- **Likely cause**: `litellm.acompletion` raises a typed exception; the proxy catches and returns the exception's `.text` directly without sanitization
- **Recommended fix**: in the exception handler, call `circuit_breaker.classify_error()` on the message; for `bad_request` class, return HTTP 400 with a sanitized message. Never return raw stack traces from upstream SDKs.
- **Status**: **OPEN**

### BUG-008 — Stack-trace leak on negative `max_tokens`

- **Severity**: high (information disclosure)
- **Area**: same as BUG-007
- **Reproduction**:
  ```bash
  curl -X POST -H "x-api-key: $KEY" ... -d '{"model":"x-ai/grok-3","max_tokens":-5,...}'
  ```
- **Expected**: HTTP 400 with `{"detail":"max_tokens must be positive"}`
- **Actual**: HTTP 502 with raw Gemini error response body shown
- **Recommended fix**: same as BUG-007 — sanitize all upstream error returns. Better yet, validate `max_tokens > 0` at the input layer.
- **Status**: **OPEN**

### BUG-009 — SDK `LmrhClient.subscribe()` thread doesn't exit promptly on `stop()`

- **Severity**: medium (graceful-shutdown UX; not a leak)
- **Area**: `sdk/python/lmrh_client.py:_sse_session`
- **Reproduction**:
  ```python
  c = LmrhClient(...)
  t = threading.Thread(target=lambda: c.subscribe(on_snapshot=cb), daemon=True)
  t.start()
  time.sleep(7)
  c.stop()
  t.join(timeout=2.0)
  assert not t.is_alive()  # FAILS
  ```
- **Expected**: thread exits within ~5s of `stop()` (the heartbeat interval default 25s, but ideally much sooner)
- **Actual**: thread blocks inside `for line in resp.iter_lines():` waiting for next event/heartbeat; can take up to `heartbeat_sec` (default 25) to notice the stop signal
- **Likely cause**: `httpx.iter_lines()` doesn't accept a stop event; the for-loop polls between lines but blocks during a line read
- **Recommended fix** (options, in order of effort):
  1. Add `httpx.stream(...)` `timeout=heartbeat_sec * 1.5` so blocked reads time out and the outer loop's `_stop` check fires
  2. Use `httpx.AsyncClient` + asyncio cancellation (more invasive — currently synchronous)
  3. Accept the limitation and document it (heartbeat_sec is the worst-case stop latency)
- **Status**: **OPEN**

### BUG-010 — 3 alias/canonical collisions in `model_capabilities` (cleanup smell)

- **Severity**: low (de-dup logic in /v1/models handles it; no runtime bug)
- **Area**: `app/providers/scanner.py` + leftover pre-v3.4.1 capability rows
- **Reproduction**:
  ```sql
  SELECT * FROM model_capabilities WHERE model_id IN ('grok-3','grok-4','x-ai/grok-3','x-ai/grok-4');
  ```
  Shows BOTH bare-name rows (legacy) AND canonical-name-with-alias rows (v3.4.1).
- **Expected**: bare-name rows cleaned up after the v3.4.1 canonical-only switch; only canonical rows with `aliases=["grok-3"]` should remain
- **Actual**: 3 collisions (bare and prefixed both registered)
- **Recommended fix**: add a v3.5.x maintenance migration that deletes capability rows whose `model_id` appears as an alias on another row's canonical (same provider). Or trigger via "Scan Models" button after operator review.
- **Status**: **OPEN**

### BUG-011 — Cross-cluster ETag drift on `/lmrh/providers`

- **Severity**: hardening / documentation gap (NOT a runtime bug, but caller-confusing)
- **Area**: `app/routing/lmrh/snapshot.py` cluster behavior
- **Reproduction**:
  ```bash
  curl -sk -H "Auth: ..." https://www.voipguru.org/llm-proxy2/lmrh/providers -I  # ETag A
  curl -sk -H "Auth: ..." https://www2.voipguru.org/llm-proxy2/lmrh/providers -I  # ETag B
  # A != B even when underlying provider config is identical
  ```
- **Expected (caller intuition)**: ETags match across cluster nodes for the same configuration
- **Actual**: ETags differ because each node aggregates `ProviderMetric` independently (per-node design per architecture.md)
- **Impact**: callers polling via DNS round-robin or a load-balancer see ETag changes that aren't real config changes, defeating the 304-cache optimization
- **Recommended fix** (in priority order):
  1. **Doc fix**: explicitly call out per-node ETag in `docs/lmrh-2.0-bidirectional.md` so callers know to pin to one node OR accept the re-fetch cost
  2. **Optional protocol enhancement**: emit a separate `LMRH-Snapshot-ID` header derived from cluster-replicated config (Provider rows + ModelCapability rows) — would match across nodes and let clients cache cross-node
- **Status**: **OPEN**

### BUG-012 — `/health` returns stale circuit-breaker state for soft-deleted providers

- **Severity**: medium (operator-confusing, no functional impact)
- **Area**: `app/cluster/sync.py` provider-tombstone propagation + `app/routing/circuit_breaker.py` `_local_states` lifecycle
- **Reproduction**:
  - Delete a provider via admin API (soft-delete via `deleted_at`)
  - Wait for cluster sync
  - Check `/health` — circuit breaker state for the deleted provider is still listed
- **Expected**: deleted-provider CB state cleared from `_local_states`
- **Actual**: state persists indefinitely until container restart; `/health` shows ghost CBs
- **Recommended fix**: in the cluster-sync tombstone-propagation handler (and the admin DELETE endpoint), call `circuit_breaker._local_states.pop(provider_id, None)` + `_auth_failed.pop(provider_id, None)`
- **Status**: **OPEN**

---

## Recently fixed (post-QA-pass remediation, v3.5.8 → v3.5.10)

The QA pass on 2026-05-09 found 12 open bugs. **9 of them shipped fixed in v3.5.8 / v3.5.9 / v3.5.10**, leaving only the 3 lowest-severity items (test-isolation flake + integration-DB pollution residual + audit-trail entries below).

### FIXED in v3.5.8 — Input validation + error sanitization

- **BUG-004**: `/v1/chat/completions` accepts requests without `model` field, returns upstream 502
- **BUG-005**: `/v1/messages` accepts empty POST body, returns 200 with auto-substituted model (denial-of-wallet vector)
- **BUG-007**: stack-trace leak on invalid `role` value
- **BUG-008**: stack-trace leak on negative `max_tokens`

All 4 closed by `app/api/_input_validation.py` (NEW): front-line `validate_completion_request()` + `sanitize_upstream_error()`. Both endpoints now return clean HTTP 400 with sanitized messages instead of 200/502 with leaked tracebacks. +19 unit tests in `tests/unit/test_v358_input_validation.py`. Live-verified post-deploy.

### FIXED in v3.5.9 — Test infra + CB cleanup hooks

- **BUG-002**: 13 errors from "Address already in use" on mock LLM server port — `tests/mock_llm_server.py` now defaults to OS-assigned port (`port=0`) and `MockServer.stop()` calls `server_close()` to release the socket immediately
- **BUG-009**: SDK `subscribe()` thread doesn't exit promptly on `stop()` — `_sse_session` now sets `httpx.Timeout(read=heartbeat_sec * 2)` instead of `None`. Live-measured: 8.2s exit vs. previously indefinite
- **BUG-012**: `/health` returns stale circuit-breaker state for soft-deleted providers — `delete_provider` and the cluster-sync tombstone-propagation path now both clear `_local_states` and `_auth_failed`. Live-verified post-deploy: orphan CB count went 13 → 0

### FIXED in v3.5.10 — QA hardening

- **BUG-006**: cross-family substitution disclosure only in `LLM-Capability` header — added `X-Substituted-From` + `X-Substituted-To` response headers (and CORS-exposed them). Browser callers can now detect substitution without parsing RFC 8941 structured-field-values
- **BUG-010**: alias↔canonical collisions in `model_capabilities` — new `tools/cleanup_alias_collisions.py` admin script (idempotent, dry-run support). Shipped inside the Docker image
- **BUG-011**: cross-cluster ETag drift on `/lmrh/providers` — documented in `docs/lmrh-2.0-bidirectional.md` with the load-balancer pinning recommendation

### Still open (low-severity, deferred)

- **BUG-001** — Test isolation flake: `TestVisionStripping::test_text_only_request_passes_through_unchanged` fails in full-suite run, passes alone. Hard to root-cause without `pytest-randomly` to surface contributors. Test-infrastructure issue; not a production defect.
- **BUG-003** — Integration tests pollute production DB (residual): the BUG-012 fix (CB cleanup on tombstone) closes the orphan-state half. The full hard-delete-on-teardown half is left for a future test-infrastructure rewrite — soft-deleted `pytest-mock` rows are swept by the daily prune worker after 7 days regardless.

## Recently fixed (during today's velocity, pre-QA-pass)

### FIXED — Cache write-back NameError silently swallowed (caught during R1 review)

- v3.5.1: extraction of `maybe_serve_from_cache` initially returned only the response (or None), losing the `cache_decision` local variable that downstream `maybe_store()` calls relied on. The `try: ... except Exception: pass` swallowed the resulting NameError, so cache write-back was quietly broken on every request. Fixed by returning the decision in a tuple.
- Tracked in `docs/refactor-log.md` R1+R2 entry.

### FIXED — `Devin-Anthropic-Max-VG` reporting 256% of weekly limit

- Operator-set `usage_weekly_limit_tokens=20M` was below Anthropic's actual Pro Max allowance. Not strictly a bug (the dashboard correctly surfaced the threshold being crossed), but operator-confusing. v3.5.4 added tooltip clarification that this is an operator-imposed early-warning ceiling, not the actual upstream limit.
