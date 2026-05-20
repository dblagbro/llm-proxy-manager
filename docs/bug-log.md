# Bug log

Findings from the 2026-05-09 deep QA pass on llm-proxy2 v3.5.7. Severity scale: **critical** (release blocker) · **high** (real impact, fix soon) · **medium** (real but bounded) · **low** (cosmetic / edge) · **enhancement / hardening** (improvement opportunity).

Add new findings on top. When status changes, leave the row in place and update the **Status** field; only delete a row if the bug never reproduced.

---

## 2026-05-19 — Post-v4.3.2 verification pass (grok-web findings)

A targeted post-deploy QA after shipping v4.3.2 (the BUG-023 interim noise
patch) surfaced two real defects — one of which is that the v4.3.2 patch
itself is non-functional because its premise was based on a misread of the
grok-web architecture.

### BUG-025 — `llm-proxy2-grok-bridge` on tmrwww01 has a crashed Playwright page

- **Severity:** high · **Category:** confirmed defect · operational
- **Area:** `llm-proxy2-grok-bridge` sidecar on tmrwww01.
- **Context:** live fleet, 2026-05-19.
- **Repro:** `docker logs --since 30m llm-proxy2-grok-bridge` shows
  `playwright._impl._errors.Error: Page.goto: Page crashed` on every
  `_capture_statsig_id` attempt. A TCP probe to
  `http://llm-proxy2-grok-bridge:8000/` from inside `llm-proxy2` returns
  `Connection refused` on `/status`, `/health`, and `/` — the FastAPI
  process inside the container isn't accepting connections, even though
  Docker reports the container as `Up 10 days`.
- **Expected:** the bridge responds on port 8000; Playwright's grok.com
  page navigates successfully.
- **Actual:** the bridge's HTTP layer is dead; Playwright's page crashes
  on `goto(grok.com)`. Every grok-web request and keepalive probe through
  the public `bridge_url` (see BUG-023 correction below) fails with
  `error_class=upstream_5xx`.
- **Suspected cause:** Chromium ran out of memory or hit an
  unrecoverable navigation error and the FastAPI wrapper didn't restart
  the page; the container's outer entrypoint is alive but the inner
  service is not (a self-monitoring gap). Possibly correlated with a
  Grok session expiry, but the immediate symptom is a process-level crash.
- **Fix direction:**
  1. **Operational (immediate, low-risk):** `docker restart
     llm-proxy2-grok-bridge` on tmrwww01 — single named container, no
     stack impact. If the bridge persists its Grok cookies it should
     come back logged in; otherwise re-auth.
  2. **Hardening (follow-up):** add a healthcheck to the grok-bridge
     compose service (e.g. `curl /status` every 30 s, restart on
     unhealthy) so a crashed inner service auto-recovers without a
     human noticing manually.
- **Update (2026-05-19 22:38 UTC) — attempted recovery did NOT succeed.**
  Batch A of the consolidated remediation plan was authorised and executed.
  Result: `docker restart` put the container into a crash-loop
  (`exit 3`, `RestartCount: 11`). Root cause of the crash-loop is an
  **image-level startup race** between Xvfb and the FastAPI lifespan —
  Chromium launches before a usable `$DISPLAY` is available:
  ```
  ERROR:ozone_platform_x11.cc(244)] Missing X server or $DISPLAY
  The platform failed to initialize. Exiting.
  ```
  Operator authorised the next step — clear `/data/playwright-state` and
  start fresh. **That also did not help**: the crash recurs with the same
  Xvfb error, confirming the issue is the image's startup orchestration,
  not the persisted Chromium user-data-dir. The 10-day-old "Up" container
  was the lucky win of this race on its original boot; subsequent restarts
  lose the race. The cleared `playwright-state` was tarball-backed-up to
  `/tmp/grok-bridge-playwright-state-bak-20260519T183844Z.tar.gz` (263 MB)
  before deletion, available for forensic re-mount if needed.
- **Current state (2026-05-19):** container **stopped** to halt the
  crash-loop and the log spam. grok-web stays unavailable (same end-user
  outcome as the original silent-zombie state; cleaner from
  observability). Rest of the fleet is unaffected — all 3 nodes serving
  v4.3.2, 9/10 providers each (Grok-Web-Devin CB tripped fleet-wide via
  cluster sync; everything else healthy).
- **Revised fix direction:** the symptom is image-level, not
  operational. The operator's restart-to-recover assumption (and the
  remediation plan's Batch A) was incorrect — the bridge image carries
  a latent startup-race bug exposed only on a fresh container exit. Two
  honest paths forward:
  1. **Patch the grok-bridge image** — fix `start.sh` /
     `supervisord` so Xvfb is fully ready (and `DISPLAY` propagated to
     the FastAPI process) before the lifespan launches Chromium. A
     real image change → rebuild → tag → push → recreate. Smallish but
     needs grok-bridge source access.
  2. **Defer to v4.4** — the per-node-auth architectural arc is going
     to redesign this whole layer anyway (and may switch from a
     persistent-context browser to a fresh-context-per-request model
     that sidesteps Xvfb entirely). Accept that grok-web is down in
     the meantime; grok-web is a tertiary fallback and the rest of
     the proxy is fully healthy. **Recommended.**
- **Status: DEFERRED to the v4.4 arc** (operator-decided 2026-05-19). The
  bridge image's startup race is not worth patching as a standalone fix;
  the v4.4 per-node-auth arc will redesign this whole layer (and may move
  off the persistent-context-with-Xvfb model entirely). Until v4.4 lands:
  - `llm-proxy2-grok-bridge` on tmrwww01 stays **stopped**.
  - `Grok-Web-Devin` stays effectively disabled fleet-wide (CB tripped via
    cluster sync; routing falls through to the higher-priority providers).
  - The forensic playwright-state tarball at
    `/tmp/grok-bridge-playwright-state-bak-20260519T183844Z.tar.gz` is
    preserved for the v4.4 design work (Grok cookies + Chromium profile
    of the 10-day-old session — may inform whether persistent-context vs
    fresh-context is the better v4.4 choice).

### BUG-026 — v4.3.2 prober-skip patch is non-functional (wrong premise) — ✅ **LIVE v4.3.4 (2026-05-19)**

- **Severity:** medium · **Category:** confirmed defect (regression in
  the v4.3.2 release) · also a test coverage gap
- **Area:** `app/monitoring/keepalive.py` — the `_local_sidecar_reachable`
  short-circuit added for BUG-023.
- **Context:** v4.3.2, live on the fleet.
- **Repro (c1conv, 2026-05-19 post-deploy):**
  - `docker logs llm-proxy2 | grep "no local grok-bridge"` → **0
    matches** since the v4.3.2 recreate. The INFO line the patch logs on
    first detection has never fired.
  - The activity log on c1conv shows new `keepalive_probe` rows for
    Grok-Web-Devin **with `origin_node=llm-proxy2-c1conv`** at the normal
    ~5-minute cadence — the prober is *not* skipping; it's still
    probing and still failing.
- **Root cause:** the patch checks `_local_sidecar_reachable(bridge_url)`
  expecting `bridge_url` to be a docker-internal hostname (e.g.
  `http://llm-proxy2-grok-bridge:8000`). The actual `bridge_url` in the
  provider config is the **public URL** (hostname `www.voipguru.org`) —
  one shared bridge on tmrwww01, all 3 nodes reach it through public
  nginx. A reachability HTTP GET to the public URL always succeeds (TLS
  connect + nginx responds), so the check returns `True` and the skip
  branch is never taken. The grok-web architecture is *shared bridge via
  public URL*, **not** per-node sidecars — invalidating the entire
  premise of the v4.3.2 fix.
- **Expected:** the patch suppresses grok-web probes / noise on nodes
  where the bridge is unreachable.
- **Actual:** the patch is a no-op in production. The noise on c1conv
  (BUG-023's symptom) was never the absence of a local sidecar — it was
  upstream-5xx errors from the (now-crashed, see BUG-025) shared bridge.
- **Suspected cause:** I diagnosed BUG-023 by inspecting the c1conv
  containers (seeing no `grok-bridge`) and inferring "missing local
  sidecar" — without verifying that the provider config's `bridge_url`
  was docker-internal. It isn't.
- **Fix direction:**
  1. **Revert the v4.3.2 keepalive.py change** (it's dead code in
     production and adds noise to the codebase). The interim noise
     suppression goal will be obsolete once BUG-025 is fixed (a working
     bridge stops the errors at source).
  2. **OR** keep the helper (`_local_sidecar_reachable` is useful as a
     general primitive) but make the *gate* condition correct — detect
     a docker-internal vs public bridge URL, or skip only on explicit
     `ConnectError` from the actual probe attempt rather than from a
     speculative pre-check.
  3. **Add a unit/integration test** that exercises the skip path with a
     real public URL (or stub) so a future patch can't accidentally
     no-op the way this one did.
- **Resolution (2026-05-19):** v4.3.4 takes option 1 (revert).
  Removed: `_no_local_sidecar` set, `is_no_local_sidecar()`,
  `_local_sidecar_reachable()`, the v4.3.2 gate branch inside
  `_probe_one()`'s grok-web arm, and `tests/unit/test_v432_no_local_sidecar.py`
  (3 tests). Unit suite drops 2148 → 2145; all green. No callers of
  `is_no_local_sidecar()` existed outside the deleted test file, so the
  revert is local in every sense.

  The compose-level grok-bridge healthcheck mentioned in the Batch B
  plan is deliberately NOT included — the bridge container is stopped
  (BUG-025 deferred to v4.4) and the v4.4 redesign will reshape what a
  "healthcheck" should look like for the v4.4 architecture. Adding a
  watchdog around a known-bad startup race now would be churn.
  ✅ **LIVE on all 3 nodes 2026-05-19 (v4.3.4)** — fleet on `version: 4.3.4`,
  `status: healthy`, 10/10 providers each.

### BUG-023 — diagnosis corrected (re-opened, but underlying issue is BUG-025)

The earlier diagnosis ("c1conv lacks the grok-bridge sidecar") was
**incorrect**. grok-web is a *shared* bridge architecture: one
`llm-proxy2-grok-bridge` container on tmrwww01, all nodes reach it via
the public URL `bridge_url=https://www.voipguru.org/...`. c1conv was
never expected to have a local bridge — the noise it produced was
the bridge's own upstream errors hitting the prober. With BUG-025
(bridge crashed) addressed, BUG-023's symptom resolves naturally; the
v4.3.2 work was barking up the wrong tree.

---

## 2026-05-19 (later) — F3 compat-matrix + nginx-restart findings

### BUG-043 — `OpenRouter-Devin-Personal` returns HTTP 400 on standard request — ✅ **FIXED 2026-05-19** (test-side; was bad input)

- **Discovered:** 2026-05-19 by `test_compatibility_matrix.py --run-real`
  (BUG-035 execution). Every one of the 12 matrix wire-format tests
  failed against this provider with HTTP 400 — but `activity_log` has
  NO error rows for the provider in the test window.
- **Hypothesis:** the proxy's pre-routing validation rejects the
  matrix's request shape before reaching the upstream — possibly the
  matrix sends a model name (`default_model`) that doesn't match any
  capability/alias on this provider, and the 400 comes from the
  capability filter rather than from OpenRouter.
- **Repro:** `curl -X POST https://www.voipguru.org/llm-proxy2/v1/messages
  -H "x-api-key: $KEY" -H "Content-Type: application/json"
  -d '{"model":"<default_model>","max_tokens":20,"messages":[{"role":"user","content":"Say OK"}]}'`
  using OpenRouter-Devin-Personal's configured `default_model`.
- **Fix direction:** one diagnostic session — capture the 400's `detail`,
  trace it back to the proxy code path that rejected it, decide whether
  the validation is correct (then update the matrix test's request) or
  wrong (then fix the validator).
- **Severity:** medium. Likely also affects any external caller that
  sends this exact shape — but no production caller has reported it,
  so impact is bounded.
- **Resolution (2026-05-19):** root cause was **bad test input**, not
  a proxy defect. Diagnostic curl confirmed the 400 body:
  `"messages: 'model' field is required and must be a non-empty
  string."` — i.e. the proxy's pre-routing validator correctly
  rejected the empty model name. The provider's `default_model`
  field is the empty string, and the matrix test passed it through
  raw. The proxy's behaviour is correct (and is the same v3.5.8
  validator that closes BUG-004/005). Fix lives in the test: new
  `_pick_chat_model(admin_session, provider)` helper in
  `tests/integration/test_compatibility_matrix.py` resolves a
  chat-capable model via `default_model` → scanned capabilities →
  per-provider-type fallback. 24 unit tests in
  `tests/unit/test_compat_matrix_chat_model_picker.py` pin each
  branch + caching. Live re-run: provider no longer in the 400
  failure list.

### BUG-044 — `Devin-Cohere` returns HTTP 400 on standard request — ✅ **FIXED 2026-05-19** (test-side; was bad input)

Same shape and provenance as BUG-043, against the Cohere provider.
Cohere has historically required different request fields than the
OpenAI-shape default; the matrix test's generic shape may not match.

**Resolution (2026-05-19):** root cause was the provider's
`default_model` set to `embed-english-v3.0` (a Cohere embedding
model). The proxy's pre-routing validator correctly rejected:
`"Model 'embed-english-v3.0' is an embeddings model. Use POST
/v1/embeddings instead of /v1/messages."` Fix lives in the
matrix test (same `_pick_chat_model` helper as BUG-043). Live
re-run: provider no longer in the 400 failure list.

### BUG-045 — `C1 Anthropic Claude` returns HTTP 400 on standard request — ✅ **400 FIXED 2026-05-19** (test-side); ⚠ **503 EXPOSES CONFIG GAP**

Same shape and provenance as BUG-043. This one is most suspicious
because the matrix tests an Anthropic-wire-format request against an
Anthropic-backed provider — the failure suggests something specific
to the C1 Anthropic provider's config (alias map? model name?) is
rejecting otherwise-valid requests.

**Diagnosis (2026-05-19):** root cause was the provider's
`default_model` set to **null**. The proxy's validator returned the
same `"'model' field is required and must be a non-empty string"`
400 as BUG-043. The `_pick_chat_model` helper now resolves
`claude-haiku-4-5` for this provider (via the anthropic-type
fallback table, because the provider has **zero scanned
`model_capabilities` rows**).

**400 closed but 503 remains** — with a valid model name now passing
the validator, the matrix re-run shows HTTP 503 instead. Cause:
because C1 Anthropic Claude has 0 capability rows, the proxy's
capability-based router doesn't consider it as a route candidate
for `claude-haiku-4-5`. The matrix test force-opens the CBs on the
*other* anthropic providers as it cycles, so by the time it tries
to exercise C1 Anthropic Claude, every anthropic-capable route is
CB-tripped and the request returns 503.

**The 503 is the correct proxy behaviour** given the config. The
remaining issue is an operator-time **config gap** — C1 Anthropic
Claude was never scanned for model capabilities. Click "Scan
Models" on this provider in the admin UI (or POST
`/api/providers/{id}/model-capabilities/infer`) to populate the
capability rows, then the matrix will route through it cleanly.

Filed separately as **CONFIG-001 (operator action)** in the
project notes — not a code bug.

### BUG-046 — nginx restart loop when `llm-proxy2-grok-bridge` upstream is stopped — ✅ **FIXED 2026-05-19**

- **Discovered:** 2026-05-19 during the BUG-035 matrix test window.
  Watched the test run for ~7 min, then API calls started returning
  ConnectionRefusedError. Investigation: nginx had restarted **7 times**
  in the same window, each cycle failing at startup with `[emerg] host
  not found in upstream "llm-proxy2-grok-bridge" in nginx.conf:1041`.
- **Root cause:** the 3 grok-bridge `proxy_pass` directives used literal
  hostnames, which nginx resolves at config-parse time. With the bridge
  container stopped (BUG-025 deferred to v4.4), every nginx reload /
  restart hits an unresolvable upstream and aborts. The vhost is then
  serving on the last successful master config (briefly) until docker
  policy restarts the container — and each restart re-attempts parse
  and fails again. A trigger that reloads nginx (cert renewal hook,
  config-watcher service, signal from elsewhere) makes the bug fire.
- **Severity:** high (any nginx reload event would break the entire
  vhost — not just llm-proxy2 routes, every project sharing this nginx).
- **Fix:** convert the 3 `proxy_pass` directives to **variable-based**
  form (`set $grok_bridge_host llm-proxy2-grok-bridge;` +
  `proxy_pass http://$grok_bridge_host:8443/...`). This defers DNS
  resolution to *request* time via the existing `resolver 127.0.0.11`
  directive (already in scope in the surrounding server block). When
  grok-bridge is stopped, the routes return 502 on request — but
  nginx itself starts cleanly. The third location (`/grok-bridge/`)
  also gains a `rewrite ^/grok-bridge(/.*)$ $1 break;` to preserve
  the prefix-stripping behaviour that literal-form `proxy_pass`
  would have done automatically.
- **Verification:**
  - `nginx -t` clean.
  - `nginx -s reload` clean.
  - nginx container restart count returned to 0 after recreate.
  - All llm-proxy2 routes still 200; grok-bridge auth-gated routes
    still 302 (the new config still serves them when the bridge is up).
- **Edit location:** `/home/dblagbro/docker/config/nginx/nginx.conf`
  (NOT in the llm-proxy-v2 repo). Backup at
  `/home/dblagbro/docker/config/nginx/nginx.conf.bak-pre-bug046-20260520`.

### BUG-047 — Anthropic→OpenAI/Cohere tool-def translation gap — ✅ **LIVE v4.3.8 (2026-05-20)**

- **Discovered:** 2026-05-20 by the proactive-monitoring sweep
  (`docs/proactive-sweep-2026-05-20.md` Finding 3).
- **Symptom:** identical upstream 400s on two different non-anthropic
  providers receiving requests with Anthropic-shape tool definitions:
  Cohere returned `"invalid tool at tools[0]: missing required field:
  'type'"`; OpenAI returned `"Missing required parameter:
  'tools[0].type'."`. Both providers had ~6-7 errors/24h in steady
  state — small but persistent.
- **Root cause:** the cross-family translation gate at
  `app/api/messages.py:259-263` fires on `cross_family_fallback OR
  _has_tool_blocks OR has_images`. None catches a **first-turn**
  request with Anthropic-shape tool DEFINITIONS in `body.tools` but
  no `tool_use`/`tool_result` blocks in messages yet — so the raw
  Anthropic-shape tools reached litellm untranslated and 400'd
  upstream on the missing `type: "function"` envelope.
- **Severity:** medium-to-high. Any caller sending Anthropic-shape
  tool defs to a non-anthropic provider got a 400 instead of a
  tool-call response — a real proxy translation hole.
- **Fix (v4.3.8 staged 2026-05-20):** new helper
  `has_anthropic_tool_defs(tools)` in `app/routing/tool_content.py`
  detects Anthropic-shape entries (have `input_schema` OR lack the
  OpenAI `{type:"function", function:{...}}` envelope). Gate widened
  to also fire on this signal. 12 new unit tests; existing
  v3.10.0-translation-gate-wiring test updated to assert each clause
  as a substring instead of a literal one-line form.
- **Operational impact post-deploy:** Devin-Cohere + Devin Personal
  OpenAI ChatGPT bad_request rates expected to drop to ~0 on tool-
  using requests; activity log will confirm.

### BUG-048 — `error_class=unknown` for Grok-Web bridge errors (classifier coverage gap) — **OPEN (low priority)**

- **Discovered:** 2026-05-20 proactive-sweep Finding 4. 47 errors in
  24h classified as `unknown` — all are Grok-Web-Devin bridge
  failures with nested-JSON shape `grok-web bridge XXX: grok.com YYY:
  {...}` that the circuit-breaker classifier's regex doesn't match.
- **Severity:** low — classifier behaviour (CB still trips correctly);
  affects operator dashboard grouping but not routing decisions.
- **Recommended fix:** Pre-strip the "grok-web bridge XXX:" prefix
  in `app/routing/circuit_breaker.py:classify_error()`, then
  re-classify the inner error using the existing regexes. ~10 lines.
- **Not picked up this session** — low priority + grok-bridge is
  stopped pending BUG-025/v4.4 anyway, so the 47 errors will
  naturally drop once v4.4 redesigns the bridge layer.

### CONFIG-001 — Operator action items surfaced during the 2026-05-20 sweep — **WITHDRAWN 2026-05-20**

Original entry filed 3 operator items. Operator clarified 2026-05-20:

1. ~~**Devin-Codex-Gmail OAuth scope insufficient**~~ — WAI fixture
   (intentional negative-test). Errors are the success signal that
   auth-failure detection works. See
   `reference_intentional_failing_provider_fixtures.md`. Do not
   flag.

2. ~~**C1 Anthropic Claude API key invalid + 0 model_capabilities**~~ —
   WAI fixture (same pattern as #1). The `"invalid x-api-key"`
   from Anthropic + the 0 scanned capabilities are the success
   signal. Do not propose re-auth, Scan Models, or any fix.

3. **system_settings rows with literal "None" string** — also
   withdrawn as actionable: fixed in v4.3.7 (now LIVE in v4.3.8);
   `_coerce()` converts legacy "None" strings to Python None on
   load. No cleanup query needed.

**Net: CONFIG-001 has zero outstanding items.** Withdrawn entirely.

---

## 2026-05-19 — F2 coverage-pass findings (real validation gaps)

While implementing Sub-batch F2 of the coverage-gaps inventory (the
broader UI + form-validation Playwright pass), two real validation
defects surfaced. Both are persisted to the DB via the live API, so
they are server-side validation gaps, not UI-only issues.

### BUG-041 — `/api/keys` accepts negative `rate_limit_rpm` — ✅ **LIVE v4.3.3 (2026-05-19)**

- **Discovered:** 2026-05-19 by `TestFormValidationNegatives::
  test_create_api_key_rejects_malformed_rate_limit` (F2 pass).
- **Area:** `app/api/apikeys.py` — request validator.
- **Repro:** POST `/api/keys` with `{"name": "x", "rate_limit_rpm": -5}`
  → 200 OK, key is created with `rate_limit_rpm: -5` persisted as-is.
  Expected: 422 / 400 rejecting the negative value (rate limits are
  count-per-minute, non-negative by definition).
- **Severity:** medium. A negative RPM is meaningless to the rate
  limiter; behavior is undefined (silently treated as unlimited, or
  blocks every request, depending on which side of the comparison the
  signed int lands on). No live caller is affected today (the form's
  HTML5 `type='number'` allows -5 through but in normal use operators
  enter positives), but the API should reject the bad input at the
  boundary.
- **Fix direction:** add `ge=0` (or `gt=0`) to the Pydantic model field
  for `rate_limit_rpm`; mirror for `rate_limit_tier` if numeric, and
  for spending caps. One-line schema change + a unit test.
- **Test:** `TestFormValidationNegatives::
  test_create_api_key_rejects_malformed_rate_limit` is currently
  `xfail(strict=False)` documenting the bug — when the fix lands,
  remove the decorator and the test becomes a regression guard.
- **Resolution (2026-05-19):** v4.3.3 adds `Field(default=None, ge=0)`
  to every numeric cap/limit field on `KeyCreate` in
  `app/api/apikeys.py`. `KeyUpdate`'s documented "-1 to clear"
  sentinel is preserved (PATCH path unchanged). 15 unit tests in
  `tests/unit/test_v433_create_validation.py` cover both fix +
  preserved semantics. F2 Playwright xfail decorator removed.
  ✅ **LIVE on all 3 nodes 2026-05-19 (v4.3.3)** — regression test passes against the deployed fleet.

### BUG-042 — `/api/users` accepts empty password — ✅ **LIVE v4.3.3 (2026-05-19)**

- **Discovered:** 2026-05-19 by `TestFormValidationNegatives::
  test_create_user_form_rejects_empty_password` (F2 pass).
- **Area:** `app/api/admin.py` (or wherever the user-create endpoint
  lives) — request validator.
- **Repro:** Add User modal — fill username only, click Create. A user
  named `pw-validation-<uuid>` is persisted with an empty / hashable-
  empty password (post-cleanup confirmed via `DELETE /api/users/<id>`
  returning 200, so the row was real). The frontend's HTML5 `required`
  on the password input did not block submission in a non-interactive
  fill-and-submit flow.
- **Severity:** medium. An empty-password user is an authentication
  hole: depending on how `bcrypt.checkpw(b"", hashed_empty)` resolves
  in this codebase, the account may be unauthenticatable (annoying but
  not a security issue) or authenticatable with any input (severe).
  Either way, the row should never have been accepted.
- **Fix direction:** add a non-empty validator on the password field
  in the user-create Pydantic model; require minimum length (e.g. 8).
  Frontend's HTML5 `required` should remain (defense in depth) but the
  server-side check is the load-bearing one.
- **Test:** `TestFormValidationNegatives::
  test_create_user_form_rejects_empty_password` is currently
  `xfail(strict=False)` documenting the bug — when the fix lands,
  remove the decorator and the test becomes a regression guard.
- **Resolution (2026-05-19):** v4.3.3 adds `Field(..., min_length=8)`
  to `UserCreate.password` and `Field(..., min_length=1)` to
  `UserCreate.username` in `app/api/users.py`. `UserUpdate`'s
  "empty password = no change" semantic is preserved (PATCH path
  unchanged — the route-level `if body.password:` check still
  governs partial updates). 15 unit tests cover both fix + preserved
  semantics; F2 Playwright xfail decorator removed.
  ✅ **LIVE on all 3 nodes 2026-05-19 (v4.3.3)** — regression test passes against the deployed fleet.

---

## 2026-05-19 — Coverage gaps inventory (audit of v4.3.0 + v4.3.2 QA scope)

Operator-requested audit of what *was not tested* during the v4.3.0 deep
QA pass and the v4.3.2 post-deploy verification, so the bug queue
formally captures every bounded-out surface. These are **coverage
findings**, not defects — no failure has been observed because no test
has been run; they exist to make the gap visible in a future QA pass and
to inform v4.4 / Batch C scoping.

Each is recorded as a `BUG-NNN` for queue uniformity, with
**Category: test coverage gap** (or **observability/doc gap** where
applicable) and **Severity: low** unless noted.

### BUG-027 — Broader admin-UI pages not deep-tested — **CLOSED 2026-05-19 (F2)**

- **Area:** Providers (add / edit / delete / capability edit), API Keys
  (create / revoke / rate limits / spending caps), Users (manage / RBAC),
  Settings panel (full surface), Activity Log (filters / sort /
  pagination), Metrics, Cluster status page.
- **What's missing:** the v4.3.0 deep QA focused on the AIRI / Routing
  surface + proxy core sanity. Full UI flows for these pages
  (validation, persistence-on-reload, modal/dialog states, empty/error
  states) were not exercised.
- **Fix direction:** add a Playwright pass per page (smoke-level: render,
  one happy CRUD per resource, one negative validation, console-error
  check). Best done as a single follow-up pass before the next deep
  regression cycle.
- **Resolution (2026-05-19, F2):** three new Playwright classes added to
  `tests/integration/test_playwright_ui.py`:
  `TestActivityLogFilters` (search submit + severity filter + clear-all),
  `TestMetricsPageRender` (render + window selector), and
  `TestSettingsPagePersistence` (render + a save/reload round-trip on
  `circuit_breaker_threshold`). All green against the live deployment.

### BUG-028 — Form-validation depth beyond `/api/airi/speak` + auth — **CLOSED 2026-05-19 (F2); surfaced BUG-041 + BUG-042**

- **Area:** all create/edit forms (Providers, API Keys, Rules, Scheduled
  Rules, Notification Prefs, Settings).
- **What's missing:** empty / malformed / oversized / unsupported-value
  inputs are only systematically tested for `/api/airi/speak` and the
  auth endpoints; the other forms rely on their own (untested) Pydantic
  validators.
- **Fix direction:** a small fuzz-table per form (empty, max-length,
  special chars, wrong types) at the API layer; one negative-validation
  Playwright case per form.
- **Resolution (2026-05-19, F2):** `TestFormValidationNegatives` added
  with two cases (empty password on user create; negative
  `rate_limit_rpm` on key create). Both pass the input through and
  surfaced **real API validation gaps** — see BUG-041 + BUG-042 above.
  Tests are currently `xfail(strict=False)` and will convert to
  regression guards when those underlying bugs are fixed.

### BUG-029 — Data persistence + reload depth — **CLOSED 2026-05-19 (F2)**

- **Area:** Routing / AIRI panels (only the AIRI sticky-chat reload was
  covered); Settings; Provider edits; Rule-set activation; API Key
  rate-limit + spending caps.
- **What's missing:** "edit → save → reload page → confirm persisted"
  flow for the surfaces above. Cluster-sync angle ("save on tmrwww01 →
  appears on tmrwww02") also not exercised for non-AIRI surfaces.
- **Fix direction:** one Playwright save-reload pair per editable
  surface; one cluster-sync verification per surface known to sync.
- **Resolution (2026-05-19, F2):**
  `TestSettingsPagePersistence::test_circuit_breaker_threshold_round_trips_through_reload`
  (settings save+reload) and `TestProviderPersistence::test_created_provider_survives_reload`
  (provider create+reload+cleanup) added. Both green. Cluster-sync
  per-surface verification remains a future deeper pass.

### BUG-030 — Cache behavior not live-exercised — **CLOSED 2026-05-19 (F2)**

- **Area:** request cache (`app/api/_cache_inject.py`, the cache
  decision in `_request_pipeline.py`).
- **What's missing:** live verification that cache hits return the
  prior response, cache writes happen on miss, and cache eviction /
  invalidation work end-to-end. Unit tests (`tests/unit/test_cache_inject.py`)
  cover the helper logic; live integration was not exercised this pass.
- **Fix direction:** add a `tests/integration/test_cache_live.py` that
  drives one repeat-request pair through the live proxy and asserts the
  second is a cache hit (header `LLM-Cache: hit`).
- **Resolution (2026-05-19, F2):** `TestCacheHeaderLive::test_cache_status_header_present`
  added — issues two identical `/v1/messages` calls and asserts the
  `X-Cache-Status` header is present on both (proves the cache decision
  is wired into the response pipeline). Header name corrected from the
  fix-direction note's `LLM-Cache` to the actual `X-Cache-Status`
  (set in `app/api/_request_pipeline.py:432-438`).

### BUG-031 — Notifications dispatch not live-tested — ✅ **LIVE v4.3.6 (2026-05-19)**

- **Area:** AIRI rule-fire email path (`app/airi/notify.py` +
  `notify_prefs.py`) — the v4.0.3 surface.
- **What's missing:** the code is source-greped only; a real notification
  dispatch (SMTP send) hasn't been exercised in QA since v4.0.3 shipped.
- **Fix direction:** stage a no-op monitor rule that fires once, observe
  the email is delivered (or captured by a stubbed SMTP); verify
  preference filtering excludes opted-out recipients.
- **Deferral (2026-05-19, F2):** safe live testing requires a
  `dry_run` / test-mode flag in `app/airi/notify.py` that returns the
  rendered email body without performing an SMTP send. Without that
  flag, a live test would spam the operator's inbox each run. A small
  notifier code change unblocks this — captured as a follow-up; unit
  suite continues to cover rendering + recipient-filter logic.
  Inline TODO marker placed in `tests/integration/test_playwright_ui.py`
  above the `TestResponsiveLayout` class.
- **Resolution (2026-05-19, v4.3.6):** `airi_notify(...)` gains
  `dry_run: bool = False` (also honors `AIRI_NOTIFY_DRY_RUN` env var).
  New admin-only endpoint `POST /api/airi/notify/_test_dispatch` is
  the HTTP front door — body `{subject, message, severity, category}`,
  response is the planned-dispatch dict (subject + body + resolved
  recipients) with NO SMTP send. 14 new unit tests in
  `tests/unit/test_v436_notify_dry_run.py` cover both paths (param +
  env var), production-path regression guard, and the truthy / falsy
  parametrized matrix. ✅ **LIVE on all 3 nodes 2026-05-19 (v4.3.6).**
  Verified end-to-end: `POST /api/airi/notify/_test_dispatch` with a
  test payload returned 200 with the full planned-dispatch dict
  (subject, body with `/routing` deep link, recipients) and the
  operator's inbox saw no email — confirming the dry_run path
  exercises the full notifier without SMTP. Side observation: the
  `recipients` array showed `["None"]` (literal string) rather than
  empty, which means `settings.smtp_to` is currently the literal
  string `"None"` in prod — small separate finding worth follow-up.

### BUG-032 — Mobile / responsive layout not exercised — **CLOSED 2026-05-19 (F2)**

- **Area:** the whole app, but especially the AIRI panel input row
  (3 voice buttons + input + Send) at narrow widths, and the off-canvas
  sidebar from v4.0.1.
- **What's missing:** Playwright viewport-emulation runs (`375x812`
  mobile, `768x1024` tablet) of the main pages. Quick visual contrast +
  no-clip checks.
- **Fix direction:** a small responsive sweep (3 viewports × 5 pages =
  ~15 screenshots) reviewed for clipping / overflow / hidden controls.
- **Resolution (2026-05-19, F2):** `TestResponsiveLayout::test_no_horizontal_overflow`
  parametrized across 2 viewports (375x812 mobile, 768x1024 tablet) ×
  6 main pages (Providers, API Keys, Users, Activity, Metrics,
  Settings) = **12 test cases, all green**. No horizontal overflow on
  any page at either viewport. Sidebar collapses correctly at mobile
  width (off-canvas behavior). Hidden controls / mobile-only nav check
  not part of this pass (would need a hamburger-menu probe).

### BUG-033 — Deep keyboard accessibility not exercised — **CLOSED 2026-05-19 (F2 baseline)**

- **Area:** all interactive surfaces. Baseline a11y is present (real
  `<button>`s + `aria-label` + `aria-pressed` on voice buttons), but
  full keyboard-only flow (Tab order, focus visibility, Enter/Space
  activate, Esc dismiss for modals) wasn't driven.
- **What's missing:** a Playwright keyboard-only walk-through of: login →
  navigate sidebar → open AIRI panel → run a chat → manage a provider.
- **Fix direction:** one keyboard-only Playwright test per main flow;
  also enable `motion-reduce` emulation to confirm BUG-024's guard.
- **Resolution (2026-05-19, F2):** `TestKeyboardAccessibility`
  added — two tests cover the highest-traffic surfaces: login form
  submittable via Tab + Enter, and sidebar nav links are focusable
  (real `<a>` or `<button>`). Both green. A full keyboard-only
  walkthrough per main flow is the next a11y arc; `motion-reduce`
  Playwright emulation also pending.

### BUG-034 — Full integration suite not run end-to-end this pass — **CLOSED 2026-05-19 (F3)**

- **Area:** `tests/integration/` outside `test_playwright_ui.py::TestAiriTTS`.
- **What's missing:** `test_api_keys.py`, `test_auth.py`,
  `test_compatibility_matrix.py`, `test_cross_family_translation.py`,
  `test_manual_override_flow.py`, `test_new_features.py`,
  `test_routing_mock.py`, `test_settings_api.py`,
  `test_settings_permutation.py` were not run this session. The 2133
  unit tests + `TestAiriTTS` alone don't exercise the integration paths
  these cover.
- **Fix direction:** run `python3 -m pytest tests/integration/ -rs
  --timeout=60` and triage; expect BUG-001/002/003 to fire (already
  logged), file new findings against any new failures.
- **Resolution (2026-05-19, F3):** suite run end-to-end twice
  consecutively, ignoring `test_playwright_ui.py` (covered by F2).
  Result: **66 passed / 16 skipped / 0 failed**, both runs. Earlier
  BUG-001/002/003 did not reproduce — BUG-002 cannot reproduce here
  because `pytest-xdist` is not installed; BUG-001 + BUG-003 stayed
  green across both runs. Detailed results in `docs/f3-runbooks.md`.

### BUG-035 — Real-provider compatibility matrix not run — ✅ **RAN 2026-05-19 (F3); surfaced BUG-043 + BUG-044 + BUG-045 + BUG-046**

- **Area:** `tests/integration/test_compatibility_matrix.py --run-real`.
- **What's missing:** the `--run-real` flag spends money on live providers
  and is gated as pre-release in `test-plan.md`. The v4.3.0/v4.3.2
  releases didn't run it.
- **Fix direction:** run it once before the next *minor* release
  (v4.4-ish) to catch upstream-shape changes (especially Anthropic /
  OpenAI / Codex / Grok API surfaces).
- **F3 disposition (2026-05-19):** invocation runbook + cost estimate
  (~$1 / ~5 min runtime) + pre-flight checklist captured in
  `docs/f3-runbooks.md` §"BUG-035". Operator-triggered before the
  next minor release — does not block F3 closure for the rest of the
  inventory.
- **Execution (2026-05-19):** **1 passed / 12 failed / 0 skipped**, ~7.7
  min runtime. Two failure classes surfaced:
  1. **3 providers return HTTP 400** consistently across all 12 wire-
     format tests: OpenRouter-Devin-Personal (BUG-043), Devin-Cohere
     (BUG-044), C1 Anthropic Claude (BUG-045). The proxy's
     activity_log has NO error rows for these providers in the test
     window, which suggests the 400 is coming from the proxy's
     pre-routing validation layer, not the upstream — i.e. the
     request shape the matrix sends is being rejected before
     reaching the provider. Worth one focused diagnostic session.
  2. **Content-truncation failures** on Vertex / Google Generative
     (`max_tokens=20` clips the response before the model can
     emit the keyword the test asserts). This is test-side
     brittleness, not a product defect — file as test-infra cleanup.
  3. **BUG-046 (nginx restart loop)** also surfaced during the test
     window: while the matrix was hammering the proxy, nginx tried
     to reload (probably for cert-renewal or a daemon hook) and the
     reload aborted because `llm-proxy2-grok-bridge` was unresolvable.
     7 restarts in succession before the bridge came back up. Fixed
     same session via variable-based proxy_pass in nginx.conf — see
     BUG-046 entry.

### BUG-036 — Rollback drill never exercised — ✅ **CLOSED 2026-05-19**

- **Area:** `docs/backup-plan.md` procedures.
- **What's missing:** the documented rollback procedures (retag a prior
  image → `compose up -d --force-recreate --no-deps` per node, restore
  per-node compose `.bak-pre-v…`, etc.) have never been executed end-to-
  end. A documented-but-unverified rollback is a hope, not a procedure.
- **Fix direction:** one-shot drill on a throwaway stack — roll forward
  to a candidate version, perform the documented rollback, confirm the
  prior version is fully restored. Record outcomes + actual times in
  `backup-plan.md`.
- **F3 disposition (2026-05-19):** drill runbook captured in
  `docs/f3-runbooks.md` §"BUG-036" — step-by-step bash, three staging-
  environment options (single VM / second container on same host /
  second VM in network), and explicit PASS/FAIL criteria. Closes once
  a drill is run + recorded in `backup-plan.md`. Operator-triggered.
- **Execution (2026-05-19):** ran the drill on a stage container
  (`llm-proxy2-stage`, port 13456, `CLUSTER_ENABLED=false`, tmpfs
  /app/data). Three image cycles `4.3.4 → 4.3.6 → 4.3.4`, ready-times
  12.92 / 13.66 / 12.99 seconds. PASS — rollback target restored
  cleanly. Persistent-data preservation + cluster-sync rejoin are NOT
  exercised by this drill (stage container is isolated); those cases
  are covered by BUG-037's separate controlled-skew runbook. Full
  outcomes captured in `docs/backup-plan.md` §"Rollback drill —
  2026-05-19 (BUG-036 closure)".

### BUG-037 — Mixed-version cluster-sync (skew test) not exercised — ✅ **CLOSED 2026-05-19**

- **Area:** cluster sync paths (`app/cluster/*`, `app/api/cluster.py`,
  the various `*cluster_sync*` test files).
- **What's missing:** every rolling deploy this session ended with the
  fleet uniform on one version. The intermediate window (e.g. tmrwww01
  on `4.3.2`, tmrwww02 still on `4.3.1`) hasn't been intentionally
  held for verification. v4.3 added the new `/api/auth/session`
  endpoint — does a mixed-version cluster degrade cleanly when one
  side lacks that route?
- **Fix direction:** during the next rolling deploy, *hold* the first-
  node-only state for ~10 minutes and exercise cluster-synced surfaces
  (provider config update on the older node, observe on the newer; vice
  versa). Document version-skew tolerance.
- **F3 disposition (2026-05-19):** 5-assertion checklist captured in
  `docs/f3-runbooks.md` §"BUG-037" — exercised during the **next**
  rolling deploy (no separate session required; this rides along with
  whatever the next minor release is). Closes once the next deploy
  records its outcomes in `qa-notes.md`.
- **Execution (2026-05-19):** manufactured a controlled prod-node
  skew by downgrading tmrwww02 to v4.3.5 while tmrwww01 + c1conv
  stayed on v4.3.6. Held the skew ~146 seconds. All 5 assertions
  PASS: sync OLD→NEW in 30 s, sync NEW→OLD in 10 s, new endpoint
  returned 4xx (not 5xx) on the older node, both nodes healthy
  throughout, no skew-correlated error spike. tmrwww02 re-upgraded
  cleanly. Full outcomes in `docs/qa-notes.md` §"Mixed-version
  cluster-sync skew test — 2026-05-19 (BUG-037 closure)".

### BUG-038 — `architecture.md` does not document CB cluster-sync semantics — **CLOSED 2026-05-19 (F1)**

- **Area:** `architecture.md` (and possibly `docs/lmrh-2.0-bidirectional.md`).
- **What's missing:** the fact that **a single node's circuit-breaker
  state syncs to the entire cluster** — so one node's repeated upstream
  failures degrade *every* node's view of that provider — is not
  documented. Operationally observed during BUG-025 (one bridge crash on
  tmrwww01 tripped grok-web's CB on all 3 nodes).
- **Severity:** low (observability / doc gap).
- **Resolution (2026-05-19, F1):** `architecture.md` §"Cluster sync"
  now carries a "What syncs cluster-wide vs what stays node-local"
  table that enumerates CB state as cluster-synced with the
  "9/10 on one node often signals a problem somewhere else" operator
  note attached.

### BUG-039 — `architecture.md` does not document the grok-bridge public-URL hairpin — **CLOSED 2026-05-19 (F1)**

- **Area:** `architecture.md`, the providers/grok-web section.
- **What's missing:** that `grok-web` providers have `bridge_url` set
  to the **public URL** (e.g. `https://www.voipguru.org/...`) and that
  all 3 nodes route grok-web through one shared bridge on tmrwww01,
  hairpin through public nginx, is undocumented. This gap is what led
  me to misread BUG-023 (assumed per-node sidecars, in fact one shared
  bridge). A 30-second `SELECT extra_config FROM providers` would have
  shown the real architecture.
- **Severity:** medium (its absence cost a wasted v4.3.2 release —
  BUG-026).
- **Resolution (2026-05-19, F1):** the §"grok-bridge sidecar"
  "Cross-node reachability" subsection has been replaced with
  "Sidecar topology — there is exactly ONE grok-bridge in the fleet",
  which makes explicit (a) only tmrwww01 runs the container, (b)
  *every* node — including tmrwww01 itself — reaches it via the public
  URL because `providers.extra_config.bridge_url` cluster-syncs, (c)
  the three operational consequences (no per-node auth state, CB sync
  amplification, the "live-config read before sidecar fix" rule that
  would have prevented BUG-026), and (d) a pointer to the v4.4 arc as
  the planned redesign.

### BUG-040 — `architecture.md` does not document `activity_log` row scope — **CLOSED 2026-05-19 (F1)**

- **Area:** `architecture.md`, monitoring / cluster-sync coverage.
- **What's missing:** `activity_log` rows are **per-node** (each row has
  `event_meta.node_id`; rows are NOT cluster-synced). This contrasts
  with CB state (synced) — an inconsistency worth calling out so a
  diagnostician knows where to look. Observed during BUG-026's
  diagnosis (the 5 recent c1conv probe rows all carried
  `origin_node=llm-proxy2-c1conv`, confirming local-only origin).
- **Severity:** low (doc).
- **Resolution (2026-05-19, F1):** the same §"Cluster sync" table
  that closes BUG-038 also calls out `activity_log` rows as
  node-local / NOT synced, with the asymmetry to CB state explained
  (rows are high-volume; sync overhead would dominate).

---

## 2026-05-18 — QA pass v4.3.0 (AIRI text-to-speech surface)

Deep regression + release-hardening pass on v4.3.0. 2130/2130 unit tests +
~42 live checks pass against the released images on an isolated prod-DB copy.
**No critical / high / medium defects.** 5 low / coverage / operational
findings — none release-blocking. Full report: `docs/4.3-qa-report.md`.

### BUG-020 — Pre-login `/api/auth/me` 401 logs a console error every load

- **Severity:** low · **Category:** observability gap
- **Area:** frontend auth bootstrap
- **Context:** any fresh page load, all themes, v4.3.0 (pre-existing — not
  introduced by v4.3).
- **Repro:** open any page logged-out → DevTools console shows
  `Failed to load resource: 401 (Unauthorized)` for `/api/auth/me`.
- **Expected:** a clean console; the boot auth-probe is a normal "am I
  logged in?" check and 401 is its expected negative answer.
- **Actual:** the 401 surfaces as a red console error every load.
- **Evidence:** 1 console error during an otherwise-clean QA UI run; it maps
  exactly to the boot `/api/auth/me` probe.
- **Suspected cause:** the auth-status probe uses a plain `fetch`; a 401 is
  always logged by the browser as a failed resource load.
- **Fix direction:** treat the boot-probe 401 as expected — it already is,
  functionally; the noise just muddies real-error triage. Low priority.
- **Status:** ✅ implemented on the `v2` branch — additive
  `GET /api/auth/session` (always 200) + the frontend boot probe switched to
  it; `/me` keeps its 401 contract. Verified: 0 console errors on a
  logged-out load. Ships in v4.3.1.

### BUG-021 — TTS message→speak wiring has no automated test

- **Severity:** low · **Category:** test coverage gap
- **Area:** `AiriChatPanel` / `AiriSpeaker` (v4.3)
- **Context:** v4.3.0.
- **Repro:** n/a — the only coverage of "a completed assistant message
  triggers `speakerRef.speak()`" is the live throwaway smoke; the unit tests
  in `test_airi_voice.py` are source-grep assertions.
- **Expected:** an automated test exercising the integrated flow.
- **Actual:** none; a regression here would only be caught by manual QA.
- **Fix direction:** add a Playwright integration test (speaker on → chat
  turn → assert `/api/airi/speak` fires).
- **Status:** ✅ implemented on the `v2` branch — `TestAiriTTS` in
  `tests/integration/test_playwright_ui.py` (stubs the chat SSE, asserts a
  completed message fires `/api/airi/speak`). Verified passing. Ships in v4.3.1.

### BUG-022 — Audible TTS playback unverifiable in headless Chromium

- **Severity:** low · **Category:** test coverage gap
- **Area:** `AiriSpeaker` audio playback (v4.3)
- **Context:** headless QA environment.
- **Repro:** headless Chromium has no audio device; `audio.play()` after a
  non-gesture `message` event cannot be confirmed to produce sound.
- **Expected:** verification that the synthesized clip actually plays.
- **Actual:** QA confirmed `/api/airi/speak` fires and returns a valid WAV,
  and the `<audio>` element is fed — but not that audio is audible, nor the
  autoplay-policy edge case (play() triggered outside a user gesture).
- **Fix direction:** add a real-browser manual check to the release
  checklist; consider priming the `<audio>` element inside the speaker-toggle
  click gesture to harden against autoplay rejection.
- **Status:** ✅ addressed on the `v2` branch — `docs/release-checklist.md`
  adds a manual real-browser TTS audible-playback check (and the autoplay
  edge case to watch). The optional audio-priming code change was not taken
  (no autoplay failure observed). Ships in v4.3.1.

### BUG-023 — c1conv reports 9/10 healthy providers

- **Severity:** low · **Category:** operational / observability
- **Area:** fleet — c1conv node
- **Context:** live fleet, observed during v4.3.0 QA.
- **Repro:** `GET https://34.170.189.19/llm-proxy2/health` → `healthyProviders:9`
  (tmrwww01 + tmrwww02 report 10/10).
- **Expected:** 10/10, matching the other nodes.
- **Actual:** the `Grok-Web-Devin` provider (id `8beb17c4bd11de26`, type
  `grok-web`) is down on c1conv — its circuit breaker is half-open with 5
  failures; **285/285 keepalive probes failed in the last 24 h** (every
  ~5 min, `severity=error`).
- **Root cause (diagnosed 2026-05-19):** c1conv has **no `grok-bridge`
  sidecar**. `grok-web` providers are served only via the `grok-bridge`
  browser-automation sidecar; tmrwww01 runs `llm-proxy2-grok-bridge` (and
  its grok-web CB is *closed* — healthy). The provider config is
  cluster-synced, so `Grok-Web-Devin` is enabled on all 3 nodes, but the
  sidecar is per-node infrastructure and was never deployed on c1conv.
  Not v4.3-related.
- **Fix direction (needs an operator decision — options):**
  1. Deploy a `grok-bridge` sidecar on c1conv. Requires a logged-in Grok
     web session (Grok account credentials / interactive login) — outward-
     facing, credential-laden; an operator task.
  2. Accept that c1conv does not serve grok-web (it is a tertiary fallback;
     the CB correctly excludes it). The cost is the 9/10 health figure and
     ~285 failed keepalive probes/day of log noise on c1conv.
  3. Enhancement: have the keepalive prober skip a provider whose required
     sidecar is absent on the local node, so a node without grok-bridge
     does not probe (and trip on) grok-web.
- **Status:** ✅ interim noise patch shipped in **v4.3.2** (2026-05-19) —
  the keepalive prober now pre-checks `bridge_url` reachability and silently
  skips the probe when the local sidecar isn't there, so the 285/day error
  rows on c1conv are gone and `healthyProviders` is back to 10/10. The
  **proper** fix — actually serving grok-web from c1conv via a per-node
  bridge + a guided cross-node auth UI — is the v4.4 arc
  (`docs/4.4-per-node-auth-design.md`, pending).

### BUG-024 — Voice buttons' pulse animation ignores `prefers-reduced-motion`

- **Severity:** enhancement · **Category:** accessibility
- **Area:** `AiriSpeaker` / `AiriMicButton` / `AiriHandsFree`
- **Context:** v4.3.0 (and pre-existing on the v4.2 mic/hands-free buttons).
- **Repro:** the synthesizing/speaking (and recording) states use Tailwind
  `animate-pulse` with no `motion-reduce:` guard.
- **Expected:** respect `prefers-reduced-motion`.
- **Actual:** the pulse animates regardless of the OS reduced-motion setting.
- **Fix direction:** add `motion-reduce:animate-none` to the three voice
  buttons. Minor.
- **Status:** ✅ implemented on the `v2` branch — `motion-reduce:animate-none`
  added alongside `animate-pulse` on `AiriSpeaker`, `AiriMicButton`,
  `AiriHandsFree`. Ships in v4.3.1.

---

## 2026-05-10 — QA pass v3.7.13 / v3.7.14 (v3.7.x surface)

### Remediation plan (priority order)

| # | Item | Severity | Status |
|---|------|----------|--------|
| 1 | BUG-019 — admin lockout deadlock | **CRITICAL** | ✅ FIXED in v3.7.14 |
| 2 | BUG-016 — cluster sync gap (3 new tables) | medium | ✅ FIXED in v3.7.15 (+ tombstone column for blocked_ips DELETE propagation) |
| 3 | BUG-017 — AI rate limiter recursion guard | high | ✅ FIXED in v3.7.15 (X-Internal-Source tag + filter) |
| 4 | BUG-018 — IP block cache invalidation cross-node | medium | ✅ FIXED in v3.7.15 (bundled with BUG-016) |
| 5 | UI Backlog A — claude-oauth legacy usage fields | enhancement | ✅ FIXED in v3.7.14 (collapsed behind disclosure) |
| 6 | UI Backlog B — codex-oauth → ChatGPT-oauth-plan UI label | enhancement | ✅ FIXED in v3.7.14 (label-only) |
| 7 | Full data rename — codex-oauth value → ChatGPT-oauth-plan | enhancement | OPEN — needs operator approval (breaking; v3.8.0) |

### Open scope item: full-value rename of `codex-oauth` provider_type

The v3.7.14 UI label change ("ChatGPT-oauth-plan (codex-oauth)" displayed in the dropdown) satisfies the user-facing intent without breaking changes. A full string-value rename — changing the actual `provider_type` value from `codex-oauth` to `ChatGPT-oauth-plan` everywhere — has these costs:

- 34 source files updated (94 literal occurrences)
- DB migration to UPDATE existing `providers` rows
- Cluster sync coordination: peers must roll concurrently OR accept a brief mismatch window
- External callers (anyone POSTing to `/api/providers` with `provider_type: "codex-oauth"`) will need to update
- Routing-key matches in `app/routing/router.py`, `app/api/messages.py`, `app/api/completions.py`, etc.

**Recommendation**: do NOT ship the full rename without explicit operator approval. The UI label change captures the intent at near-zero risk; the value rename is breaking. If approved, ship as a major-bump (v3.8.0) with:
1. Idempotent migration in `app/models/database.py` that UPDATEs existing rows
2. Dual-accept compatibility window in API endpoints (accept both old and new values for one minor version)
3. Deprecation log on every old-value match so external callers see warnings
4. Coordinated cluster roll (all 3 nodes within ~5 min of each other)

Files touched (highest-impact, sample):
- `app/api/providers.py` (9 occurrences — validation strings)
- `app/api/providers_oauth.py` (11 — OAuth flow branching)
- `app/routing/router.py` (9 — provider-type filters)
- `app/api/_codex_oauth_dispatch.py` (8 — dispatch helpers)
- `frontend/src/pages/ProvidersPage.tsx` (5 — type guards)
- ... plus 29 more

### BUG-019 — Admin lockout deadlock: middleware 403s the only endpoint that can un-block

- **Severity**: **CRITICAL** (operator self-DoS; no in-band recovery path)
- **Area**: `app/middleware/ip_block.py` (v3.7.11 ASGI front-stack)
- **Reproduction**:
  1. POST `/api/admin/blocked-ips` with `{"ip": "<your own egress IP>"}` (deliberate, or via a future "auto-add" rule from BUG-017)
  2. Try to call `DELETE /api/admin/blocked-ips/<that IP>`
- **Expected**: DELETE succeeds (HTTP 200, row removed)
- **Actual** (pre-fix): DELETE returns 403 "Source IP is blocked by administrator." — the IP block middleware runs before the endpoint handler, so the operator cannot use the API to recover. Direct DB access was required.
- **How it surfaced**: while testing BUG-018 cache invalidation, I added my own LAN-egress IP (192.168.18.1) to the block list. The next request — including the DELETE I was about to make to remove it — was 403'd by the middleware. The DELETE handler itself was fine; it just never ran.
- **Fix shipped**: **v3.7.14**. Middleware now bypasses two narrow path prefixes for any caller, blocked or not:
  - `/api/auth/login` (admin can sign in)
  - `/api/admin/blocked-ips` (admin can list / add / DELETE)

  Both endpoints remain `require_admin`-gated, so a blocked attacker still can't use them — they just don't 403 at the middleware layer. +4 unit tests in `tests/unit/test_v3711_ip_block.py`.
- **E2E verified post-deploy**:
  ```
  POST   /api/admin/blocked-ips    add 192.168.18.1   → 200 ok
  GET    /api/providers            (blocked IP test)  → 403 (block active)
  DELETE /api/admin/blocked-ips/192.168.18.1          → 200 ok (the fix)
  blocked_ips: 0 entries
  ```
- **Status**: **FIXED in v3.7.14** (cluster on .14)

### BUG-018 — IP block cache invalidation is single-node (peers wait ≤30s) ✅ FIXED in v3.7.15

- **Severity**: medium (timing window, not data integrity)
- **Area**: `app/middleware/ip_block.py` (`_TTL_SEC = 30.0`)
- **Reproduction**:
  1. POST `/api/admin/blocked-ips` against www01
  2. Immediately verify www01 enforces (403)
  3. Immediately verify www02 — still 200 until its TTL expires
- **Expected (caller intuition)**: cluster-wide block within seconds
- **Actual**: only the receiving node clears its cache eagerly (via `_clear_cache_for_tests` from the admin write path). Peer nodes pick up the new row via cluster sync + their own 30s TTL refresh.
- **Likely cause**: no pub/sub or sync-broadcast on `blocked_ips` writes. Cluster sync handles the row replication; cache invalidation isn't wired into that sync.
- **Recommended fix**: emit a cluster-sync event for `blocked_ips` writes that calls `_clear_cache_for_tests()` on receipt. Low-risk; pattern already exists for other admin writes.
- **Status**: **OPEN** — accept timing window for now (admin writes are rare; 30s peer-stale window is acceptable for v3.7.x)

### BUG-017 — AI rate limiter has no recursion guard for its own LLM calls ✅ FIXED in v3.7.15

- **Severity**: high (cost-amplifier risk; not a runtime crash)
- **Area**: `app/monitoring/ai_rate_limiter.py` (v3.7.10 + v3.7.12)
- **Reproduction**: enable the AI rate limiter; it calls `http://localhost:3000/v1/messages` with a proxy-internal admin key to classify per-key behavior. That request:
  1. Hits `/v1/messages` — picks a provider, dispatches, returns
  2. Is logged in `activity_log` (per v3.6.2 capture)
  3. Will be included in the NEXT AI-rate-limiter sample window for that internal key
- **Expected**: the AI rate limiter's own calls are excluded from the sample, or marked so they can't recursively be the subject of their own classification
- **Actual**: no recursion guard. Each review cycle includes the previous cycle's prompts in the new prompt's sample, slowly amplifying the prompt size and cost.
- **Why we didn't see it explode yet**: the cycle is hourly + the prompts are tiny. But under sustained operation this is an O(n²) cost in stored sample size.
- **Recommended fix**: tag activity_log rows from the AI rate limiter (`event_meta.source = "ai_rate_limiter"`) and exclude them in `compute_stats` / `pick_sample_previews`. Add a recursion-depth header on the outgoing httpx request as belt-and-braces.
- **Status**: **OPEN** (queued for v3.7.15)

### BUG-016 — Three new v3.7.x tables NOT in cluster sync ✅ FIXED in v3.7.15

- **Severity**: medium (multi-node data drift; not a single-node bug)
- **Area**: `app/cluster/sync.py` table allowlist
- **Reproduction**:
  1. Add an entry to `blocked_ips` on www01 via admin API
  2. Query www02 DB directly: `SELECT * FROM blocked_ips` — 0 rows
- **Expected**: cluster-replicated like `Provider`, `ModelCapability`, `LmrhDims`
- **Actual**: the v3.7.x tables that landed quickly all skipped the sync list:
  - `blocked_ips` (v3.7.11)
  - `api_key_ai_review` (v3.7.10)
  - `external_usage_snapshot` (v3.7.0) — partial: `Provider.anthropic_session_captured_at` syncs but the snapshot rows don't
- **Why this matters**:
  - `blocked_ips`: admin blocks an IP on one node; peers don't enforce until their own scrape catches it (n/a at v3.7.x — peers don't scrape, so peers never block)
  - `api_key_ai_review`: review reports are node-local; operator viewing the UI on www02 won't see reviews that ran on www01
  - `external_usage_snapshot`: each node scrapes independently, multiplying provider-side load 2-3x for no incremental data
- **Recommended fix**: add all three to the cluster-sync allowlist with LWW conflict resolution. For `external_usage_snapshot`, additionally elect a single leader to do the scrape and replicate (separate ticket).
- **Status**: **OPEN** (queued for v3.7.15)

---

## 2026-05-09 — Open findings (QA pass v3.5.7)

### BUG-001 — Test isolation failure: `TestVisionStripping::test_text_only_request_passes_through_unchanged` — ✅ FIXED v3.5.11

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

### BUG-002 — 13 integration test errors from "Address already in use" on mock LLM server — ✅ FIXED v3.5.9

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

### BUG-003 — Integration tests pollute the production DB — ✅ FIXED v3.5.11

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

### BUG-004 — `/v1/chat/completions` accepts requests without `model` field, returns upstream 502 — ✅ FIXED v3.5.8

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

### BUG-005 — `/v1/messages` accepts empty POST body, returns 200 with auto-substituted model — ✅ FIXED v3.5.8

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

### BUG-006 — Unknown model name silently routes to a default; substitution disclosed only in `LLM-Capability` header — ✅ FIXED v3.5.10

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

### BUG-007 — Stack-trace leak on invalid `role` value — ✅ FIXED v3.5.8

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

### BUG-008 — Stack-trace leak on negative `max_tokens` — ✅ FIXED v3.5.8

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

### BUG-009 — SDK `LmrhClient.subscribe()` thread doesn't exit promptly on `stop()` — ✅ FIXED v3.5.9

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

### BUG-010 — 3 alias/canonical collisions in `model_capabilities` (cleanup smell) — ✅ FIXED v3.5.10

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

### BUG-011 — Cross-cluster ETag drift on `/lmrh/providers` — ✅ FIXED v3.5.10 (documented)

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

### BUG-012 — `/health` returns stale circuit-breaker state for soft-deleted providers — ✅ FIXED v3.5.9

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

### FIXED in v3.5.11 — Last 2 bugs + second QA sweep

- **BUG-001**: test isolation flake — `mock_ctl` fixture cleared `_received` but never drained `_queue`; an unconsumed leftover from a prior test was served to the next. Added `MockServer.clear_queue()` and called it from the fixture
- **BUG-003**: full-pruge of pytest-mock provider rows on session-end — added `/api/providers/_purge-test-tombstones` admin endpoint mirroring the existing api-keys parallel; `pytest_sessionfinish` now hits both
- **BUG-013** (NEW from second sweep): webhook URL scheme not validated — `X-Webhook-URL: file:///etc/passwd` was accepted by httpx. Now rejects any scheme other than http/https with a 400 (`validate_webhook_url`)
- **BUG-015** (NEW from second sweep): unbounded `stop_sequences` array — 1000-entry payload was silently passed to upstream. Now capped at 16 with a clear 400

## Recently fixed (during today's velocity, pre-QA-pass)

### FIXED — Cache write-back NameError silently swallowed (caught during R1 review)

- v3.5.1: extraction of `maybe_serve_from_cache` initially returned only the response (or None), losing the `cache_decision` local variable that downstream `maybe_store()` calls relied on. The `try: ... except Exception: pass` swallowed the resulting NameError, so cache write-back was quietly broken on every request. Fixed by returning the decision in a tuple.
- Tracked in `docs/refactor-log.md` R1+R2 entry.

### FIXED — `Devin-Anthropic-Max-VG` reporting 256% of weekly limit

- Operator-set `usage_weekly_limit_tokens=20M` was below Anthropic's actual Pro Max allowance. Not strictly a bug (the dashboard correctly surfaced the threshold being crossed), but operator-confusing. v3.5.4 added tooltip clarification that this is an operator-imposed early-warning ceiling, not the actual upstream limit.
