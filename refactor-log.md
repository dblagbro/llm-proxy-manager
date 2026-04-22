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
