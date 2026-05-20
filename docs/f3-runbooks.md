# F3 runbooks (coverage gaps inventory · sub-batch F3)

**Status (2026-05-19):**
- **BUG-034** — **DONE.** Integration suite is clean under the default
  invocation (2 consecutive runs, 66 passed / 16 skipped / 0 failed).
- **BUG-035** — runbook below. Operator-triggered.
- **BUG-036** — runbook below. Needs a throwaway stack.
- **BUG-037** — checklist below. Exercised during the next rolling deploy.

The actual closure of 035/036/037 requires operator-time and (035) a
small per-call budget; this document captures the procedures so they
can be run at-will without re-discovering the invocation pattern.

---

## BUG-034 results (2026-05-19)

```
$ python3 -m pytest tests/integration/ --ignore=tests/integration/test_playwright_ui.py --timeout=120 -q
... 66 passed, 16 skipped, 65 warnings in 74.30s
$ python3 -m pytest tests/integration/ --ignore=tests/integration/test_playwright_ui.py --timeout=120 -q
... 66 passed, 16 skipped, 65 warnings in 64.61s
```

Both consecutive runs: **0 failures, 0 errors.** The 16 skipped are the
`@pytest.mark.real_providers` tests (gated on `--run-real`).

Earlier-pinned BUG-001 (flaky test under full-suite runs), BUG-002
(static port collision under parallel runs), BUG-003 (test cleanup
leaves rows) were **not reproduced** under this invocation:
- BUG-001: the suite is order-independent and stable across two runs.
- BUG-002: requires `pytest-xdist` (not installed in this environment);
  cannot be exercised here. The original report stands as a "if you
  enable parallel runs, you'll hit port collisions" — a real
  observation, just not currently reachable.
- BUG-003: the session-finish hook in `tests/conftest.py` purges
  test-key tombstones + test-provider tombstones on each session end;
  the F2 pass confirmed it works in practice (test users / providers
  / keys with `pw-*` prefixes were cleaned up).

**Recommendation:** keep BUG-001/002/003 as OPEN low-severity Batch D
items (the underlying fragility patterns still exist; they're just
not surfacing under current usage). If parallel runs are ever
introduced in CI, BUG-002 must be fixed first.

---

## BUG-035 — real-provider compatibility matrix (operator-triggered)

The `tests/integration/test_compatibility_matrix.py` module is gated on
`pytest.mark.real_providers`; it is skipped by default. To exercise
the full matrix:

```bash
python3 -m pytest tests/integration/test_compatibility_matrix.py --run-real -v
```

**What it does** — 13 parametrized tests; each iterates every provider
returned by `/api/providers` and exercises one shape:

| Test | Shape exercised |
|---|---|
| `test_anthropic_non_stream_all_providers` | `/v1/messages` non-stream, `max_tokens=20` |
| `test_openai_non_stream_all_providers` | `/v1/chat/completions` non-stream, similar |
| `test_anthropic_stream_all_providers` | SSE stream over `/v1/messages` |
| `test_openai_stream_all_providers` | SSE stream over `/v1/chat/completions` |
| `test_llm_capability_header_all_providers` | header presence check |
| `test_task_completeness[...]` × 4 | small task prompts (coding, debugging, config, troubleshooting) |
| `test_multi_turn_context` | one 3-turn conversation |
| `test_tool_call_structure_all_providers` | one native tool-use roundtrip |
| `test_stream_non_stream_content_equivalent` | content equivalence between modes |
| `test_generate_matrix` | summary tabulation only — no extra API calls |

**Cost estimate** — assuming the live fleet has ~10 enabled non-mock
providers and each shape calls each provider once with `max_tokens=20`:

- Total non-streamed call count: ~10 × 12 shapes ≈ **120 paid calls**.
- Per-call input: ~5–50 tokens. Per-call output: ≤20 tokens.
- Subscription-tier providers (`claude-oauth`, `codex-oauth`,
  `grok-web`) consume quota slots, not dollars.
- Per-token providers (Gemini, OpenRouter, mock): roughly under
  **$1.00 total** at current pricing for output tokens.

**Pre-flight checklist before invoking:**

1. **No active operator-driven work** on the proxy (the test cycles
   circuit-breakers per provider; if a caller is in flight, they may
   see one 503 per matrix iteration).
2. **A budget headroom check** — `SELECT day_cost_usd FROM api_keys
   WHERE name LIKE 'pytest-%'` should not be near any cap.
3. **`grok-web` provider state** — currently the bridge is **stopped**
   (BUG-025 deferred). Those tests will skip via the bridge-502 path
   built into the helper. No fix needed; just expect skips.

**When to run** — pre-flight before any minor release (v4.4-class).
The output is the v4.4 sign-off artefact for upstream-shape
compatibility.

**Where to record results** — append to `docs/4.x-qa-report.md` (or the
relevant release's QA report) once invoked. The test prints a
provider-coverage summary line in `TestCompatibilitySummary`.

---

## BUG-036 — rollback drill (needs a throwaway stack)

`docs/backup-plan.md` documents the rollback approach but the procedure
has never been exercised end-to-end. This runbook turns that into a
concrete drill.

### Required staging environment

Three options, in order of fidelity-vs-effort:

1. **Single-node throwaway VM** (lowest cost) — a fresh Ubuntu 22.04
   VM with Docker installed; clone `/home/dblagbro/docker/` (the
   compose tree); run only the `llm-proxy2` container + a stub
   nginx. Drops fleet semantics but exercises the image-retag +
   recreate path.
2. **A second instance on tmrwww02 under a different compose name**
   (e.g. `llm-proxy2-stage`) — sharing the host but isolated by
   container name. The most realistic without standing up new infra.
3. **A second VM in the same docker network as one production node**
   — full fidelity including cluster sync; requires DNS + a peer
   entry in the cluster table.

Recommendation: **option 2** for the first drill (one host, no extra
infra), then option 3 only if/when cluster-sync rollback semantics need
verification beyond "image retag still routes traffic."

### Drill steps

```bash
# 0. Snapshot the staging stack's current state
STAGE=llm-proxy2-stage  # container name on the chosen host
sudo docker inspect ${STAGE} | jq '.[0].Config.Image' > /tmp/drill-pre-image.txt
sudo docker exec ${STAGE} curl -s http://127.0.0.1:3000/health | jq '.version' > /tmp/drill-pre-version.txt

# 1. Roll FORWARD to a candidate release (the same one you intend to
#    eventually ship). This must be a known-good image already pushed
#    to dockerhub.
CANDIDATE=4.4.0-rc1     # set to the actual tag under drill
sudo docker pull dblagbro/llm-proxy2:${CANDIDATE}
sudo docker tag dblagbro/llm-proxy2:${CANDIDATE} llm-proxy2:latest
sudo docker compose up -d --force-recreate --no-deps ${STAGE}
sleep 8

# Confirm the forward roll
sudo docker exec ${STAGE} curl -s http://127.0.0.1:3000/health \
  | jq '.status, .version'   # expected: "healthy", "${CANDIDATE}"

# 2. Now exercise the rollback procedure from docs/backup-plan.md §"Rollback approach"
PRIOR=$(cat /tmp/drill-pre-image.txt | tr -d '"')   # e.g. dblagbro/llm-proxy2:4.3.2
sudo docker tag ${PRIOR} llm-proxy2:latest
sudo docker compose up -d --force-recreate --no-deps ${STAGE}
sleep 8

# 3. Verify rollback (§"Restore verification steps")
sudo docker exec ${STAGE} curl -s http://127.0.0.1:3000/health \
  | jq '.status, .version, .healthyProviders'

# Expected pass criteria:
#   .status        == "healthy"
#   .version       == $(cat /tmp/drill-pre-version.txt)
#   .healthyProviders matches the pre-drill snapshot
```

### Pass / fail criteria

- **PASS**: `/health` returns the pre-drill version, status healthy, and
  the same `healthyProviders` count.
- **FAIL**: any of the above mismatches, OR the rollback step requires
  manual intervention not documented in `backup-plan.md`. Update
  `backup-plan.md` with whatever step was missing.

### Where to record results

- Append a "Rollback drill — YYYY-MM-DD" section to `docs/backup-plan.md`
  with: timings (forward + rollback in seconds), any manual steps that
  were missing from the docs, and a PASS/FAIL verdict.
- Closes BUG-036 once the section exists.

---

## BUG-037 — mixed-version cluster-sync skew test (next rolling deploy)

Currently every rolling deploy ends with the fleet uniform on one
version — the intermediate state (e.g. tmrwww01 on N+1, tmrwww02 on
N) is never deliberately held for verification. Schedule this for the
**next minor release** (e.g. v4.4.0).

### During the next rolling deploy, hold the first-node state for ~10 min

```bash
# Step 1 of the rolling deploy as usual: deploy to tmrwww01.
sudo docker pull dblagbro/llm-proxy2:NEW_VERSION
sudo docker tag dblagbro/llm-proxy2:NEW_VERSION llm-proxy2:latest
sudo docker compose up -d --force-recreate --no-deps llm-proxy2

# Confirm tmrwww01 is on NEW_VERSION and tmrwww02 is still on prior.
curl -s https://www.voipguru.org/llm-proxy2/health | jq '.version, .clusterPeers[]?.url, .clusterPeers[]?.lastVersion'

# >>> HOLD HERE for ~10 minutes; do NOT proceed to tmrwww02 yet. <<<
```

### Assertions during the hold

Run from any admin terminal (no compose changes needed):

1. **Provider config edits propagate from the older node to the newer node** —
   on tmrwww02 (prior version), toggle a non-critical provider via
   `/api/providers/{id}/toggle` and confirm tmrwww01 reflects the change
   within ~90 seconds (sync cycle is 60 s).
2. **And vice versa** — make the inverse change on tmrwww01 (newer) and
   confirm tmrwww02 picks it up.
3. **A new endpoint that exists only on NEW_VERSION** (e.g.
   `/api/auth/session` was new in v4.3.1 — pick whatever's new in
   NEW_VERSION) **returns 404 on the older node**, NOT a 500 — confirm
   the older node degrades cleanly when it lacks a route.
4. **`/health` on both nodes returns `status:healthy`** — neither node
   should be marking the other unhealthy during the skew window.
5. **No spike in `/api/activity` rows with severity=error or warning**
   that correlates with the skew window.

### Pass / fail criteria

- **PASS**: 5/5 assertions hold for the entire 10-minute hold.
- **FAIL**: any assertion fails. Capture (a) which side did what; (b)
  the wire-protocol error; (c) which version-skew code path is
  responsible.

### After the hold

Proceed to tmrwww02 + c1conv as normal. Append a "Mixed-version skew
test — YYYY-MM-DD" section to `docs/qa-notes.md` with the assertion
outcomes. Closes BUG-037.

---

## Summary

| BUG | Status | Action required |
|---|---|---|
| BUG-034 | **DONE 2026-05-19** | None — suite is currently clean |
| BUG-035 | **OPERATOR-TRIGGERED** | Run before next minor release. ~$1 budget, ~5 min runtime |
| BUG-036 | **NEEDS STAGING** | Set up `llm-proxy2-stage` container; ~30 min drill |
| BUG-037 | **NEXT ROLLING DEPLOY** | Hold the mid-deploy state for 10 minutes; run 5 assertions |
