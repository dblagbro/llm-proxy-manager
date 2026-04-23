# Refactor Log

## 2026-04-22 — Incremental architectural refactor (maintainability pass)

### Motivation
Prior feature additions (rate limiting, streaming metrics, cluster spending-cap sync, vision
stripping, multi-tag tool emulation) left three clusters of duplication and mixed responsibility
that would compound as the codebase grows.

### Change 1: Shared metrics/circuit-breaker outcome helper
**New file**: `app/monitoring/helpers.py` — `record_outcome(db, provider_id, model, *, success, ...)`

**Before**: The pattern `record_failure/record_success + estimate_cost + record_request` appeared
6 times across `api/messages.py` and `api/completions.py` (regular stream, CoT stream, non-stream
× 2 files), with minor variations that could drift over time.

**After**: Single call-site in each handler. Adding a new tracking field (e.g. model version,
region, request ID) requires one change instead of six.

**Files changed**: `api/messages.py`, `api/completions.py`, `monitoring/helpers.py` (new)

### Change 2: Extract apply_sync to app/cluster/sync.py
**New file**: `app/cluster/sync.py` — `apply_sync()`, `get_peer_total_cost()`, `_peer_key_costs`

**Before**: `cluster/manager.py` (375 lines) mixed two distinct concerns: peer lifecycle
(heartbeat, ping, status reporting) and data synchronisation (120+ line `apply_sync` handling
users, API keys, providers, settings with last-write-wins merge logic).

**After**: `manager.py` owns the peer mesh (~200 lines); `sync.py` owns the incoming data merge.
`apply_sync` is re-exported from `manager.py` for backwards compatibility with existing callers.
`auth/keys.py` updated to import `get_peer_total_cost` directly from `sync.py`.

**Files changed**: `cluster/manager.py`, `cluster/sync.py` (new), `auth/keys.py`

### Change 3: Consolidate image detection/stripping
**New file**: `app/api/image_utils.py` — `has_images_anthropic`, `strip_images_anthropic`,
`has_images_openai`, `strip_images_openai`

**Before**: Four private functions split across `api/messages.py` and `api/completions.py`,
duplicating the detection/replacement logic with slightly different placeholder text.

**After**: Both endpoint files import from a single utility module. Future changes (e.g. adding
video support, changing placeholder format) require one edit.

**Files changed**: `api/messages.py`, `api/completions.py`, `api/image_utils.py` (new)

### Net result
- 3 new focused modules totalling ~200 lines
- ~180 lines removed from existing files
- No behaviour changes; all 9 providers healthy post-deploy

---

## 2026-04-22 — Incremental architectural refactor (second pass)

### Motivation
Two mixed-responsibility violations remained after the first pass: SSE serialization code
living inside a reasoning pipeline file, and model-family knowledge embedded in a routing
protocol file.

### Change 1: Wire format serialization extracted to `cot/sse.py`
**New file**: `app/cot/sse.py`

**Before**: `cot/pipeline.py` contained 8 SSE helper functions (lines 82–117) whose sole
job was producing Anthropic SSE event bytes — a serialization concern in a pipeline
execution file. `cot/tool_emulation.py` independently reimplemented the same Anthropic
event format (plus OpenAI variants) in its response generators (lines 215–347), with no
shared foundation.

**After**: `cot/sse.py` is the single source of truth for all SSE event serialization:
- 8 Anthropic SSE primitives (`sse_thinking_start/delta/stop`, `sse_text_start/delta/stop`,
  `sse_message_delta`, `sse_done`) — used by `pipeline.py`
- 8 Anthropic + OpenAI response generators (`anthropic_tool_sse`, `openai_tool_response`,
  etc.) — imported directly by `api/messages.py` and `api/completions.py`

`pipeline.py`: −40 lines (352→312), now pure reasoning logic with no format code.
`tool_emulation.py`: −136 lines (346→210), now pure emulation logic (prompt building,
normalization, parsing, LLM call). Removed unused `secrets` and `AsyncIterator` imports.

**Dependency direction preserved**: `api/ → cot/sse.py → (none)`.
Changing the Anthropic SSE format now requires one file edit.

**Files changed**: `cot/pipeline.py`, `cot/tool_emulation.py`, `cot/sse.py` (new),
`api/messages.py`, `api/completions.py`

### Change 2: Model heuristics extracted to `routing/capability_inference.py`
**New file**: `app/routing/capability_inference.py`

**Before**: `routing/lmrh.py` mixed two unrelated concerns: the LMRH routing protocol
(parse, score, rank, build header) and `infer_capability_profile` — a 54-line knowledge
base of model naming conventions that acts as a fallback when no DB record exists.
These change for different reasons: the protocol evolves with the LMRH spec; the
inference evolves with new model families.

**After**: `lmrh.py` (288→235 lines) contains only the LMRH protocol. Adding a new
model family means editing one clearly named file. `router.py` (the sole caller) now
imports `infer_capability_profile` directly from `routing/capability_inference.py`.

**Files changed**: `routing/lmrh.py`, `routing/capability_inference.py` (new),
`routing/router.py`

### Net result
- 2 new focused modules totalling ~250 lines
- ~175 lines removed from existing files (net zero growth)
- 131/131 tests pass; no behaviour changes

---

## 2026-04-22 — Incremental architectural refactor (third pass)

### Motivation
Two broken imports introduced by the second pass, one private-API coupling, and one
incomplete wire-format extraction left behind by that pass.

### Change 1: Fix broken `infer_capability_profile` imports (critical bug)
`infer_capability_profile` was moved from `routing/lmrh.py` to
`routing/capability_inference.py` in pass 2, but two callers were missed.
The existing Docker container was not rebuilt after pass 2, so integration tests
continued to pass against the old binary — but the next rebuild would have crashed
the app on startup with `ImportError`.

**Files fixed**: `providers/scanner.py:12`, `api/providers.py:207`

### Change 2: Promote private routing helpers to public API
`providers/scanner.py` imported `_build_litellm_model` and `_build_litellm_kwargs`
(underscore-prefixed) from `routing/router.py` — a legitimate caller using private
names. Renamed to `build_litellm_model` / `build_litellm_kwargs` throughout.

**Files changed**: `routing/router.py`, `providers/scanner.py`

### Change 3: Complete wire format consolidation in `cot/sse.py`
`api/messages.py` still owned `_FINISH_TO_STOP` (finish-reason→stop-reason map) and
`_to_anthropic_response()` (non-streaming response builder) — wire format concerns that
belong alongside the SSE generators. Both moved to `cot/sse.py` (renamed to
`FINISH_TO_STOP` and `to_anthropic_response`, dropping the underscore prefix since
they are now public exports). `api/messages.py` now imports them from `cot.sse`.

**Files changed**: `api/messages.py`, `cot/sse.py`

### Net result
- No new files; ~50 lines removed from `api/messages.py`
- All 131 tests pass; all 3 nodes healthy post-deploy

---

## 2026-04-22 — Incremental architectural refactor (fourth pass)

### Motivation
Two remaining mixed-responsibility issues: HMAC security primitives embedded in the
peer-lifecycle file, and an identical 8-line header-parsing block copy-pasted across
both endpoint handlers.

### Change 1: Extract `cluster/auth.py` — HMAC security primitives
**New file**: `app/cluster/auth.py` — `sign_payload`, `verify_payload`,
`verify_cluster_request`, `auth_headers_for`

**Before**: `cluster/manager.py` mixed peer lifecycle (heartbeat, ping, push-sync,
status, startup) with HMAC signing/verification. The auth functions had accumulated
private aliases (`_sign`, `_verify`, `_auth_headers`) — a code smell showing they
were originally internal but escaped without a clean interface. Two `sync` imports
sat in the middle of the file with `# noqa: E402` markers despite no actual
circular dependency preventing top-of-file placement.

**After**: `manager.py` (244→207 lines) owns only peer lifecycle. `cluster/auth.py`
owns the signing primitives. `api/cluster.py` imports auth functions from
`cluster.auth` directly. Mid-file imports moved to the top of `manager.py`;
private aliases removed; `_sign(body)` call-site updated to `sign_payload(body)`.

Auth scheme changes (algorithm, header names) now touch `cluster/auth.py` only.
Peer behaviour changes touch `cluster/manager.py` only.

**Files changed**: `cluster/manager.py`, `cluster/auth.py` (new), `api/cluster.py`

### Change 2: Deduplicate CoT header parsing + fix lazy import
**Extracted to**: `cot/pipeline.py` — `parse_cot_request_headers(x_cot_iterations,
x_cot_verify) -> tuple[int|None, bool|None]`

**Before**: An identical 8-line block for parsing `X-Cot-Iterations` and
`X-Cot-Verify` request headers appeared verbatim in both `api/messages.py` and
`api/completions.py`. Adding a new CoT request header would require two edits in
two files. Also: `api/providers.py` held a lazy `from app.routing.capability_inference
import infer_capability_profile` inside the endpoint body — a code-smell import
that hid the module's dependencies.

**After**: Both endpoint files call `parse_cot_request_headers(...)` (one line each).
`api/providers.py` imports `infer_capability_profile` at the file top with all other
imports.

**Files changed**: `cot/pipeline.py`, `api/messages.py`, `api/completions.py`,
`api/providers.py`

### Net result
- 1 new file (`cluster/auth.py`, ~40 lines)
- ~55 lines removed from existing files
- 174/174 non-UI tests pass; all 3 nodes healthy post-deploy

---

## 2026-04-23 — Short-term improvements S1–S4

### S1: Wire `record_outcome` → `log_event`
**File**: `app/monitoring/helpers.py`

Every LLM request now writes an `ActivityLog` entry via `log_event()` in `record_outcome()`.
Success events include `model`, `in_tok`, `out_tok`, `cost_usd`, `latency_ms` in the
`metadata` JSON field. Failure events include `model` and `error` (truncated to 200 chars).
No schema migration required — `ActivityLog.event_meta` already accepts JSON.

Previously every API request was invisible to the activity feed; now all 6 call-sites get
activity entries automatically via the single `record_outcome` helper.

### S2: `Retry-After` header on 429 rate-limit responses
**File**: `app/auth/keys.py`

The rate-limit `HTTPException(429, ...)` now includes `headers={"Retry-After": "60"}`.
Clients that respect this header (Claude Code, Cursor, Continue, curl) will wait the correct
amount before retrying rather than hammering the endpoint.

### S3: `GET /v1/models` endpoint
**New file**: `app/api/models.py`; registered in `app/main.py`

Returns OpenAI-format model listing (`{"object": "list", "data": [...]}`) of all models
from enabled providers. Each entry carries `id` (model_id), `object: "model"`, `created`,
and `owned_by` (provider name). Unauthenticated — standard practice for self-hosted proxies.
Required by Claude Code, Cursor, Continue, and any tool that auto-discovers available models.

### S4: `X-Resolved-Model` response header
**Files**: `app/api/messages.py`, `app/api/completions.py`, `app/main.py`

Both endpoint handlers now include `X-Resolved-Model: <litellm_model_string>` in all
responses (streaming and non-streaming). Added to `expose_headers` in CORS middleware so
browser clients can read it. Useful for debugging routing decisions and for clients that
want to log exactly which model variant was used.

### Net result
- 1 new file (`api/models.py`, ~35 lines)
- Small targeted edits to 4 existing files
- 113/113 unit tests pass; all 3 nodes healthy post-deploy
